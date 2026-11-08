"""Optional real-provider acceptance audit using labelled synthetic RU/KZ/EN only.

Run ``python -m server.semantic_check`` with the application's model environment.
This calls the real adapter; it never downloads models, reads DEV or stores answers.
Passing these small fixtures is a regression gate, not proof of general accuracy.
"""
import argparse
import json
import os
import time

from server.evaluation import PROMPT_REVISION, infer, score, think_enabled
from server.presets import official_checklist


LANGUAGES = ("RU", "KZ", "EN")
SCENARIOS = ("completed", "departure", "bot_only_greeting")
EXTRA_SCENARIOS = ("farewell_only",)  # Explicit opt-in; existing nine-case gate unchanged.
TEXT = {
    "RU": {
        "question": "Здравствуйте! Где на сайте посмотреть общие тарифы?",
        "solution": "Здравствуйте! Меня зовут Анна. На официальном сайте откройте меню «Клиентам», затем раздел «Тарифы». Там опубликованы общие тарифы.",
        "plain_solution": "На официальном сайте откройте меню «Клиентам», затем раздел «Тарифы». Там опубликованы общие тарифы.",
        "thanks": "Спасибо, нашёл раздел, вопрос решён!",
        "farewell": "Рада помочь! Мы нашли общие тарифы в разделе «Тарифы». Если появятся вопросы, обращайтесь. До свидания!",
        "farewell_only": "Пожалуйста! До свидания!",
        "hello": "Здравствуйте!", "greeting": "Здравствуйте! Меня зовут Анна. Чем могу помочь?",
        "disconnect": "Клиент потерял соединение до того, как сформулировал запрос. Чат прерван автоматически; оператор не имел возможности завершить разговор.",
        "closed": "Чат завершён после прощания оператора.",
    },
    "KZ": {
        "question": "Сәлеметсіз бе! Сайттан жалпы тарифтерді қайдан көруге болады?",
        "solution": "Сәлеметсіз бе! Менің атым Анна. Ресми сайтта «Клиенттерге» мәзірін, содан кейін «Тарифтер» бөлімін ашыңыз. Жалпы тарифтер сол жерде жарияланған.",
        "plain_solution": "Ресми сайтта «Клиенттерге» мәзірін, содан кейін «Тарифтер» бөлімін ашыңыз. Жалпы тарифтер сол жерде жарияланған.",
        "thanks": "Рақмет, бөлімді таптым, сұрағым шешілді!",
        "farewell": "Көмектескеніме қуаныштымын! Жалпы тарифтерді «Тарифтер» бөлімінен таптық. Қосымша сұрақтарыңыз болса, хабарласыңыз. Сау болыңыз!",
        "farewell_only": "Оқасы жоқ! Сау болыңыз!",
        "hello": "Сәлеметсіз бе!", "greeting": "Сәлеметсіз бе! Менің атым Анна. Сізге қалай көмектесе аламын?",
        "disconnect": "Клиент сұрағын тұжырымдамай тұрып байланыс үзілді. Чат автоматты түрде тоқтатылды; оператордың әңгімені аяқтауға мүмкіндігі болмады.",
        "closed": "Оператор қоштасқаннан кейін чат аяқталды.",
    },
    "EN": {
        "question": "Hello! Where on the website can I find the general fees?",
        "solution": "Hello! My name is Anna. On the official website, open the Clients menu, then the Fees section. The general fees are published there.",
        "plain_solution": "On the official website, open the Clients menu, then the Fees section. The general fees are published there.",
        "thanks": "Thank you, I found the section, that answers my question!",
        "farewell": "Glad to help! We found the general fees in the Fees section. Please contact us if you need any more help. Goodbye!",
        "farewell_only": "You are welcome! Goodbye!",
        "hello": "Hello!", "greeting": "Hello! My name is Anna. How can I help you?",
        "disconnect": "The client disconnected before formulating a request. The chat was interrupted automatically; the operator had no opportunity to close the conversation.",
        "closed": "The chat ended after the operator said goodbye.",
    },
}


def synthetic_fixture(language, scenario):
    """Return the conversation and independent, source-based factual expectations."""
    words = TEXT[language]
    if scenario not in SCENARIOS + EXTRA_SCENARIOS:
        raise ValueError("unknown synthetic scenario")
    cid = f"semantic-{language.lower()}-{scenario}"
    if scenario == "bot_only_greeting":
        turns = [('client', 'question'), ('bot', 'greeting'), ('operator', 'plain_solution'),
                 ('client', 'thanks'), ('operator', 'farewell'), ('system', 'closed')]
    elif scenario in ("completed", "farewell_only"):
        turns = [('client', 'question'), ('operator', 'solution'), ('client', 'thanks'),
                 ('operator', 'farewell'), ('system', 'closed')]
    else:
        turns = [('client', 'hello'), ('operator', 'greeting'), ('system', 'disconnect')]
    if scenario == "farewell_only":
        turns[3] = ("operator", "farewell_only")
    conversation = {
        "conversation_id": cid, "language": language, "channel": "agent chat",
        "dataset_group_id": "SYNTHETIC_SEMANTIC_CHECK", "audio_url": None,
        "label": "демонстрационный пример, синтетический диалог проверки модели",
        "transcript": [{"id": f"{cid}:{i}", "timestamp": f"00:{i * 10:02d}",
                        "speaker": role, "text": words[key]}
                       for i, (role, key) in enumerate(turns)],
    }
    expectations = ({"01": scenario in ("completed", "farewell_only"), "02": None, "03": None, "04": 20,
                     "05": True, "06": 10, "07": scenario != "farewell_only", "08": False}
                    if scenario != "departure" else
                    {"01": True, "04": None, "07": None, "08": False})
    # These specific facts must be backed by the operator turns that contain them.
    evidence = {"01": [f"{cid}:1"]}
    if scenario in ("completed", "farewell_only"):
        evidence.update({"04": [f"{cid}:1"], "06": [f"{cid}:1", f"{cid}:3"], "07": [f"{cid}:3"]})
    elif scenario == "bot_only_greeting":
        evidence = {"01": [f"{cid}:2"], "04": [f"{cid}:2"],
                    "06": [f"{cid}:2", f"{cid}:4"], "07": [f"{cid}:4"]}
    return conversation, expectations, evidence


def check_semantics(languages=LANGUAGES, scenarios=SCENARIOS):
    """Run all selected fixtures; never return model explanations or transcripts.

    Adapter errors and score rejection fail the structural gate. Once structurally
    valid, answers and evidence are checked against facts independently of totals.
    """
    checklist = official_checklist()
    # Snapshot only public configuration, never endpoint credentials or reasoning.
    # Invalid think configuration remains an ordinary failed provider check below.
    try:
        think = think_enabled()
    except ValueError:
        think = None
    provider = {"type": "ollama", "model": os.environ.get("CQA_MODEL", "").strip(),
                "prompt_revision": PROMPT_REVISION, "think": think}
    reports = []
    for language in languages:
        for scenario in scenarios:
            conversation, expected, evidence = synthetic_fixture(language, scenario)
            started = time.monotonic()
            report = {"language": language, "scenario": scenario,
                      "structural_status": "failed", "semantic_status": "not_run"}
            try:
                answers = infer(checklist, conversation)
                result = score(checklist, answers, conversation)
            except Exception as error:
                report["failure"] = ("model_not_configured" if isinstance(error, ValueError)
                                     and str(error) == "модель не настроена"
                                     else "provider_or_structure_failed")
                report["elapsed_seconds"] = round(time.monotonic() - started, 3)
                reports.append(report)
                # Avoid repeated slow failures when the provider is unavailable.
                return _summary(reports, provider)
            rows = {row["criterion_id"]: row for row in result["criteria"]}
            assertions = []
            for ident, value in expected.items():
                actual = rows[ident]["answer"]
                # bool and number must not compare equal merely through Python's 1 == True.
                matched = type(actual) is type(value) and actual == value
                if isinstance(value, int) and not isinstance(value, bool):
                    matched = type(actual) in (int, float) and actual == value
                assertions.append({"criterion_id": ident, "check": "answer",
                                   "expected": value, "actual": actual,
                                   "status": "passed" if matched else "failed"})
                if ident in evidence:
                    backed = bool(set(evidence[ident]) & set(rows[ident]["evidence_message_ids"]))
                    if value is False and rows[ident].get("missed"):
                        span = rows[ident].get("checked_span")
                        if span:
                            positions = {m["id"]: i for i, m in enumerate(conversation["transcript"])}
                            backed = backed or any(positions[span["start_message_id"]] <= positions[mid]
                                                   <= positions[span["end_message_id"]]
                                                   for mid in evidence[ident])
                    assertions.append({"criterion_id": ident, "check": "operator_evidence",
                                       "status": "passed" if backed else "failed"})
            report.update({"structural_status": "passed",
                           "semantic_status": "passed" if all(a["status"] == "passed" for a in assertions) else "failed",
                           "assertions": assertions,
                           "elapsed_seconds": round(time.monotonic() - started, 3)})
            reports.append(report)
    return _summary(reports, provider)


def _summary(reports, provider):
    structural = all(r["structural_status"] == "passed" for r in reports)
    semantic = all(r["semantic_status"] == "passed" for r in reports)
    return {"status": "passed" if structural and semantic else "failed",
            "synthetic_only": True, "provider": provider,
            "structural_status": "passed" if structural else "failed",
            "semantic_status": ("failed" if any(r["semantic_status"] == "failed" for r in reports)
                                else "passed" if semantic else "not_run"),
            "checks": reports,
            "limitation": "Synthetic regression gate only; real DEV judgments and evidence require independent review."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", choices=LANGUAGES, action="append")
    parser.add_argument("--scenario", choices=SCENARIOS + EXTRA_SCENARIOS, action="append")
    args = parser.parse_args(argv)
    report = check_semantics(args.language or LANGUAGES, args.scenario or SCENARIOS)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
