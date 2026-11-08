"""Independent review tests derived from the official checklist text.

Synthetic data only. Expectations come from tests/fixtures/checklist.txt
(weights, N/A redistribution, HALF_UP rounding, gross flag without zeroing),
not from observed behaviour.
"""
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import tempfile
import unittest

from server.app import Application
from server.evaluation import score, validate_checklist
from server.presets import official_checklist
from server.storage import Store

CONV = {"transcript": [{"id": "s:0", "speaker": "client"}, {"id": "s:1", "speaker": "operator"}]}


def ans(cid, value, **extra):
    a = {"criterion_id": cid, "answer": value, "explanation": "Synthetic review fixture",
         "evidence_message_ids": ["s:1"]}
    if value is None:
        a["na_reason"] = "Synthetic applicability condition"
        a["evidence_message_ids"] = []
    a.update(extra)
    return a


def official_answers(**overrides):
    out = []
    for c in official_checklist()["criteria"]:
        value = c["max"] if c["type"] == "numeric" else c["id"] != "08"
        out.append(ans(c["id"], overrides.get(c["id"], value)))
    return out


def custom(criteria, decimals=2, policy="flag"):
    return {"name": "Synthetic custom", "provenance": "custom", "criteria": criteria,
            "rules": {"decimals": decimals, "gross_error_policy": policy}}


def boolean(cid, weight, **extra):
    return {"id": cid, "title": cid, "type": "boolean", "weight": weight, **extra}


class CustomRoundingTests(unittest.TestCase):
    def total(self, weights_true, weights_false, decimals):
        criteria = [boolean(f"t{i}", w) for i, w in enumerate(weights_true)]
        criteria += [boolean(f"f{i}", w) for i, w in enumerate(weights_false)]
        answers = [ans(c["id"], c["id"].startswith("t")) for c in criteria]
        return score(custom(criteria, decimals), answers, CONV)["total"]

    def test_half_up_at_zero_and_one_decimals(self):
        # 1/8 = 12.5 -> 13 (HALF_UP, banker's would give 12).
        self.assertEqual(self.total([1], [7], 0), 13)
        # 1/16 = 6.25 -> 6.3 (float round() gives 6.2).
        self.assertEqual(self.total([1], [15], 1), 6.3)
        # 5/16 = 31.25 -> 31.3; 1/40 = 2.5 -> 3 at zero decimals.
        self.assertEqual(self.total([5], [11], 1), 31.3)
        self.assertEqual(self.total([1], [39], 0), 3)

    def test_six_decimals_repeating(self):
        self.assertEqual(self.total([1], [2], 6), 33.333333)
        self.assertEqual(self.total([2], [1], 6), 66.666667)

    def test_float_weights_keep_exact_maximum_and_half_up(self):
        self.assertEqual(self.total([0.1, 0.2, 0.3], [], 2), 100)
        # 0.1 / (0.1 + 0.7) = 12.5% exactly in decimal weights.
        self.assertEqual(self.total([0.1], [0.7], 0), 13)

    def test_sections_use_configured_decimals(self):
        criteria = [boolean("a", 1, section="X"), boolean("b", 7, section="X"), boolean("c", 1, section="Y")]
        result = score(custom(criteria, 0), [ans("a", True), ans("b", False), ans("c", True)], CONV)
        self.assertEqual([s["total"] for s in result["sections"]], [13, 100])
        self.assertEqual(result["total"], 22)  # 2/9 = 22.22 -> 22

    def test_invalid_rules_rejected(self):
        for rules in [{"decimals": 7}, {"decimals": -1}, {"decimals": True}, {"decimals": 2.0},
                      {"gross_error_policy": "ignore"}]:
            checklist = custom([boolean("a", 1)])
            checklist["rules"] = rules
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                validate_checklist(checklist)

    def test_custom_signed_scale_normalizes_to_full_weight_at_top(self):
        crit = [{"id": "n", "title": "n", "type": "numeric", "weight": 4, "min": -2, "max": 2}]
        for value, expected in [(-2, 0), (0, 50), (1, 75), (2, 100)]:
            with self.subTest(value=value):
                self.assertEqual(score(custom(crit), [ans("n", value)], CONV)["total"], expected)

    def test_gross_error_when_requires_boolean_criterion(self):
        crit = [{"id": "n", "title": "n", "type": "numeric", "weight": 1, "min": 0, "max": 1,
                 "gross_error": True, "gross_error_when": True}]
        with self.assertRaises(ValueError):
            validate_checklist(custom(crit))
        with self.assertRaises(ValueError):
            validate_checklist(custom([boolean("a", 1), boolean("g", 0, gross_error_when=True)]))


class OfficialEdgeTests(unittest.TestCase):
    def setUp(self):
        self.checklist = official_checklist()

    def test_gross_flag_with_maximal_na_keeps_total(self):
        answers = official_answers(**{"02": None, "03": None, "04": None, "07": None, "08": True})
        result = score(self.checklist, answers, CONV)
        self.assertTrue(result["gross_error"])
        self.assertEqual(result["applicable_weight"], 40)  # 10 + 15 + 15
        self.assertEqual(result["total"], 100)
        answers[0]["answer"] = False  # 30/40
        self.assertEqual(score(self.checklist, answers, CONV)["total"], 75)

    def test_gross_criterion_never_na_and_boolean_only(self):
        for value in (None, 1, 0, "true"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                score(self.checklist, official_answers(**{"08": value}), CONV)

    def test_row_fields_for_scale_steps_and_na(self):
        answers = official_answers(**{"02": None, "04": 5, "06": 7})
        rows = score(self.checklist, answers, CONV)["criteria"]
        by = {r["criterion_id"]: r for r in rows}
        self.assertEqual(by["04"]["normalized_score"], .25)
        self.assertEqual(by["04"]["score"], 25)
        self.assertEqual(by["06"]["normalized_score"], .7)
        self.assertIsNone(by["02"]["score"])
        self.assertEqual(by["02"]["effective_weight"], 0)
        self.assertEqual(by["02"]["weighted_score"], 0)
        self.assertEqual(by["08"]["effective_weight"], 0)
        applicable = [r for r in rows if r["answer"] is not None and r["weight"] > 0]
        self.assertAlmostEqual(sum(r["effective_weight"] for r in applicable), 1)
        self.assertAlmostEqual(by["01"]["effective_weight"], 10 / 85)

    def test_gross_section_has_no_total(self):
        result = score(self.checklist, official_answers(**{"08": True}), CONV)
        gross_section = result["sections"][-1]
        self.assertEqual(gross_section["name"], "Грубые нарушения")
        self.assertIsNone(gross_section["total"])
        self.assertEqual(gross_section["applicable_weight"], 0)

    def test_zero_total_policy_only_in_custom_copy(self):
        # Official preset flags only; a custom copy may opt into zeroing.
        copy = deepcopy(self.checklist)
        copy.update(provenance="custom", rules={"decimals": 2, "gross_error_policy": "zero_total"})
        self.assertEqual(score(copy, official_answers(**{"08": True}), CONV)["total"], 0)
        self.assertEqual(score(copy, official_answers(**{"08": False}), CONV)["total"], 100)
        self.assertEqual(score(self.checklist, official_answers(**{"08": True}), CONV)["total"], 100)

    def test_gross_error_when_false_trigger(self):
        crit = [boolean("a", 1), boolean("g", 0, gross_error=True, gross_error_when=False)]
        self.assertTrue(score(custom(crit), [ans("a", True), ans("g", False)], CONV)["gross_error"])
        self.assertFalse(score(custom(crit), [ans("a", True), ans("g", True)], CONV)["gross_error"])

    def test_reference_fraction_total_for_mixed_answers(self):
        values = {"01": False, "02": None, "03": True, "04": 15, "05": False, "06": 4, "07": None}
        weights = {"01": 10, "02": 15, "03": 20, "04": 20, "05": 15, "06": 15, "07": 5}
        maxima = {"04": 20, "06": 10}
        num = sum(Fraction(int(v)) / maxima.get(k, 1) * weights[k] for k, v in values.items() if v is not None)
        den = sum(weights[k] for k, v in values.items() if v is not None)
        expected = float(Fraction(int(num * 100 * 100 / den + Fraction(1, 2)), 100))
        self.assertEqual(score(self.checklist, official_answers(**values), CONV)["total"], expected)
        self.assertEqual(expected, 51.25)  # (20 + 15 + 6) / 80


class AppealReviewTests(unittest.TestCase):
    def setUp(self):
        runtime = Path(__file__).resolve().parents[1] / ".local" / "tests"
        runtime.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=runtime)
        self.app = Application(Store(Path(self.tmp.name) / "review.sqlite3"), provider=self.provider)
        self.app.dispatch("POST", "/api/imports", {"source": "json", "payload": [
            {"conversation_id": "rv", "language": "RU", "channel": "agent chat", "audio_url": None,
             "transcript": [{"speaker": "operator", "timestamp": "00:01", "text": "Синтетика"}]}]})
        self.evaluation = self.app.dispatch("POST", "/api/evaluations",
                                            {"conversation_id": "rv", "checklist_id": "official-cqa-v1"})
        self.original = deepcopy(self.evaluation["original"])

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def provider(checklist, _conversation):
        answers = []
        for c in checklist["criteria"]:
            value = c["max"] if c["type"] == "numeric" else c["id"] != "08"
            if c["id"] == "01":
                value = False
            answers.append({"criterion_id": c["id"], "answer": value, "explanation": "Синтетика",
                            "evidence_message_ids": ["rv:0"]})
        return answers

    def appeal(self, cid):
        return self.app.dispatch("POST", "/api/appeals", {"evaluation_id": self.evaluation["id"],
                                                          "criterion_id": cid, "comment": "Синтетика"})

    def answer(self, cid, value, **extra):
        a = next(deepcopy(a) for a in self.original["answers"] if a["criterion_id"] == cid)
        a.update(answer=value, **extra)
        return a

    def review(self, appeal, answer):
        return self.app.dispatch("POST", f"/api/appeals/{appeal['id']}/review",
                                 {"decision": "change", "reason": "Синтетика", "answer": answer})

    def test_sequential_appeals_recompute_from_final_and_keep_original(self):
        self.assertEqual(self.original["result"]["total"], 90)
        first = self.review(self.appeal("06"), self.answer("06", 4))  # 90 - 15 + 6 = 81
        self.assertEqual(first["evaluation"]["final"]["result"]["total"], 81)
        second_appeal = self.appeal("02")
        self.assertEqual(second_appeal["original_result"], self.original["result"])
        second = self.review(second_appeal, self.answer("02", None, na_reason="Данных хватало",
                                                        evidence_message_ids=[]))
        # (20 + 20 + 15 + 6 + 5) / 85 = 77.647 -> 77.65; 06 change from first appeal retained.
        self.assertEqual(second["evaluation"]["final"]["result"]["total"], 77.65)
        self.assertEqual(second["appeal"]["review"]["previous_result"]["total"], 81)
        stored = self.app.dispatch("GET", f"/api/evaluations/{self.evaluation['id']}")
        self.assertEqual(stored["original"], self.original)

    def test_invalid_replacements_rejected_without_mutation(self):
        bad = [self.answer("05", None, na_reason="N/A not allowed for 05", evidence_message_ids=[]),
               self.answer("05", 1),
               dict(self.answer("06", 7), criterion_id="04")]
        for answer in bad:
            appeal = self.appeal("05") if answer["criterion_id"] == "05" else self.appeal("06")
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                self.review(appeal, answer)
            self.assertEqual(self.app.dispatch("GET", f"/api/appeals/{appeal['id']}")["status"], "pending")
            self.app.store.put("appeals", appeal["id"], dict(appeal, status="withdrawn-for-test"))
        self.assertEqual(self.app.dispatch("GET", f"/api/evaluations/{self.evaluation['id']}"), self.evaluation)

    def test_review_uses_evaluated_checklist_version(self):
        edited = official_checklist()
        edited["criteria"][0]["weight"] = 50
        edited["family_id"] = "official-cqa"
        v2 = self.app.dispatch("POST", "/api/checklists", edited)
        self.assertEqual(v2["provenance"], "custom")
        self.assertNotEqual(v2["id"], "official-cqa-v1")
        result = self.review(self.appeal("01"), self.answer("01", True))
        self.assertEqual(result["evaluation"]["checklist_id"], "official-cqa-v1")
        self.assertEqual(result["evaluation"]["final"]["result"]["total"], 100)
        self.assertEqual(result["evaluation"]["final"]["result"]["provenance"], "official")

    def test_gross_appeal_flag_only(self):
        result = self.review(self.appeal("08"), self.answer("08", True, gross_error=False))
        final = result["evaluation"]["final"]["result"]
        self.assertTrue(final["gross_error"])
        self.assertEqual(final["total"], self.original["result"]["total"])
        self.assertFalse(self.original["result"]["gross_error"])


if __name__ == "__main__":
    unittest.main()
