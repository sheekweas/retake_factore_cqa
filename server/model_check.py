"""Real-provider smoke check using synthetic conversations only; never reads DEV.

Run from the project root: python -m server.model_check [--language RU|KZ|EN].
No test provider/fallback, persistence, model download or credentials are involved.
"""
import argparse
import json
import time

from server.evaluation import infer, score
from server.presets import official_checklist


EXAMPLES = {
    "RU": (
        "Здравствуйте! Где посмотреть общие тарифы?",
        "Здравствуйте! Меня зовут Анна. Общие тарифы доступны в разделе «Тарифы» "
        "на официальном сайте компании. Могу ещё чем-нибудь помочь? До свидания!",
    ),
    "KZ": (
        "Сәлеметсіз бе! Жалпы тарифтерді қайдан көруге болады?",
        "Сәлеметсіз бе! Менің атым Анна. Жалпы тарифтер компанияның ресми "
        "сайтындағы «Тарифтер» бөлімінде бар. Тағы көмектесе аламын ба? Сау болыңыз!",
    ),
    "EN": (
        "Hello! Where can I find the general fees?",
        "Hello! My name is Anna. General fees are available in the Fees section "
        "of the company's official website. Can I help with anything else? Goodbye!",
    ),
}


def synthetic_conversation(language):
    question, answer = EXAMPLES[language]
    cid = f"model-check-{language.lower()}"
    return {
        "conversation_id": cid, "language": language, "channel": "agent chat",
        "dataset_group_id": "SYNTHETIC_MODEL_CHECK", "audio_url": None,
        "label": "демонстрационный пример, синтетический диалог проверки модели",
        "transcript": [
            {"id": f"{cid}:0", "timestamp": "00:00", "speaker": "client", "text": question},
            {"id": f"{cid}:1", "timestamp": "00:10", "speaker": "operator", "text": answer},
        ],
    }


def check_model(languages):
    """Fail fast; returned reports exclude prompts, URLs and provider bodies."""
    checklist = official_checklist()
    reports = []
    for language in languages:
        conversation = synthetic_conversation(language)
        started = time.monotonic()
        try:
            answers = infer(checklist, conversation)
            result = score(checklist, answers, conversation)
        except Exception as error:
            # Even unexpected provider failures must not expose configuration or text.
            reports.append({"language": language, "status": "failed",
                            "elapsed_seconds": round(time.monotonic() - started, 3)})
            message = ("модель не настроена: задайте CQA_MODEL и запустите локальный сервис."
                       if isinstance(error, ValueError) and str(error) == "модель не настроена"
                       else "Проверка модели не прошла: проверьте CQA_MODEL, локальный сервис "
                            "и структурированный ответ.")
            return {"status": "failed", "synthetic_only": True, "checks": reports,
                    "message": message + " Тестовая подмена не используется."}
        reports.append({"language": language, "status": "passed",
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "criterion_count": len(result["criteria"]), "total": result["total"],
                        "gross_error": result["gross_error"]})
    return {"status": "passed", "synthetic_only": True, "checks": reports,
            "message": "Реальный провайдер вернул структурно допустимые оценки. "
                       "Смысловая корректность и качество RU/KZ/EN требуют проверки человеком."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", choices=list(EXAMPLES), action="append",
                        help="Check only selected languages (default: RU, KZ, EN)")
    args = parser.parse_args()
    report = check_model(args.language or list(EXAMPLES))
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
