"""Official checklist and mathematical boundaries; all conversations synthetic."""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from itertools import product
import unittest

from server.evaluation import score, validate_checklist
from server.presets import official_checklist


class OfficialChecklistTests(unittest.TestCase):
    def setUp(self):
        self.checklist = official_checklist()
        self.conversation = {"transcript": [
            {"id": "synthetic:0", "speaker": "client", "text": "Synthetic test question"},
            {"id": "synthetic:1", "speaker": "operator", "text": "Synthetic test answer"},
        ]}
        self.answers = [
            {"criterion_id": c["id"], "answer": c.get("max", True) if c["id"] != "08" else False,
             "explanation": "Synthetic fixture; not a model quality assessment.",
             "evidence_message_ids": ["synthetic:1"]}
            for c in self.checklist["criteria"]
        ]

    def test_exact_structure_weights_applicability_and_scales(self):
        validated = validate_checklist(self.checklist)
        self.assertEqual([c["id"] for c in validated["criteria"]], [f"{i:02}" for i in range(1, 9)])
        self.assertEqual([c["weight"] for c in validated["criteria"]], [10, 15, 20, 20, 15, 15, 5, 0])
        self.assertEqual(sum(c["weight"] for c in validated["criteria"]), 100)
        self.assertEqual([c["id"] for c in validated["criteria"] if c["allow_na"]], ["02", "03", "04", "07"])
        self.assertTrue(all(c.get("na_condition") for c in validated["criteria"] if c["allow_na"]))
        self.assertEqual(validated["criteria"][3]["allowed_values"], [0, 5, 10, 15, 20])
        self.assertEqual(validated["criteria"][5]["allowed_values"], [0, 4, 7, 10])
        self.assertEqual(validated["rules"], {"decimals": 2, "gross_error_policy": "flag"})
        self.assertEqual(score(self.checklist, self.answers, self.conversation)["total"], 100)

    def test_discrete_steps_allowed_and_intermediates_rejected(self):
        for index, valid in [(3, [0, 5, 10, 15, 20]), (5, [0, 4, 7, 10])]:
            for value in valid:
                with self.subTest(index=index, value=value):
                    self.answers[index]["answer"] = value
                    row = score(self.checklist, self.answers, self.conversation)["criteria"][index]
                    self.assertEqual(row["answer"], value)
                    self.assertEqual(row["allowed_values"], valid)
            for value in [True, False, -1, .5, 1, 6, 8, 11, 19, 21, "7"]:
                if type(value) is int and value in valid:
                    continue
                with self.subTest(index=index, invalid=value), self.assertRaises(ValueError):
                    self.answers[index]["answer"] = value
                    score(self.checklist, self.answers, self.conversation)
            self.answers[index]["answer"] = valid[-1]

    def test_gross_error_is_answer_without_zeroing_or_independent_override(self):
        for answer, separate in [(True, False), (False, True), (True, True), (False, False)]:
            with self.subTest(answer=answer, separate=separate):
                self.answers[7].update(answer=answer, gross_error=separate)
                result = score(self.checklist, self.answers, self.conversation)
                self.assertEqual(result["gross_error"], answer)
                self.assertEqual(result["criteria"][7]["gross_error"], answer)
                self.assertEqual(result["total"], 100)
                self.assertEqual(result["criteria"][7]["weighted_score"], 0)

    def test_na_redistribution_rounding_and_sections(self):
        self.answers[1].update(answer=None, na_reason="Данных для ответа хватало с самого начала.")
        self.answers[2].update(answer=None, na_reason="Запрос не требовал идентификации.")
        self.answers[3]["answer"] = 5
        self.answers[5]["answer"] = 7
        result = score(self.checklist, self.answers, self.conversation)
        # (10 + 5 + 15 + 10.5 + 5) / (100 - 15 - 20) * 100.
        self.assertEqual(result["applicable_weight"], 65)
        self.assertEqual(result["total"], 70)
        self.assertEqual(result["sections"][1]["total"], 25)
        self.answers[0]["answer"] = False
        self.assertEqual(score(self.checklist, self.answers, self.conversation)["total"], 54.62)

    def test_na_disallowed_criteria_and_missing_reason(self):
        for index in range(8):
            candidate = deepcopy(self.answers)
            candidate[index]["answer"] = None
            with self.subTest(index=index), self.assertRaises(ValueError):
                score(self.checklist, candidate, self.conversation)
            candidate[index]["na_reason"] = "Synthetic applicability explanation"
            if index in (1, 2, 3, 6):
                score(self.checklist, candidate, self.conversation)
            else:
                with self.assertRaises(ValueError):
                    score(self.checklist, candidate, self.conversation)

    def test_all_official_combinations_match_exact_decimal_totals_and_sections(self):
        # Exhaust every permitted answer/N/A combination for weighted criteria.
        # Decimal reference is independent of the scorer's rational arithmetic.
        choices = [[False, True], [False, True, None], [False, True, None],
                   [0, 5, 10, 15, 20, None], [False, True], [0, 4, 7, 10],
                   [False, True, None]]
        count = 0
        def reference(indices, values):
            weight = Decimal(0)
            points = Decimal(0)
            for i in indices:
                if values[i] is None:
                    continue
                c = self.checklist['criteria'][i]
                w = Decimal(c['weight'])
                weight += w
                points += Decimal(int(values[i])) * w / Decimal(c.get('max', 1))
            return float((points * 100 / weight).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)) if weight else None
        for values in product(*choices):
            for answer, value in zip(self.answers, values):
                answer['answer'] = value
                if value is None:
                    answer['na_reason'] = 'Synthetic combination: applicability assumed for arithmetic test.'
            result = score(self.checklist, self.answers, self.conversation)
            self.assertEqual(result['total'], reference(range(7), values), values)
            for section, indices in zip(result['sections'], [(0, 1), (2, 3), (4, 5, 6), ()]):
                self.assertEqual(section['total'], reference(indices, values), (values, indices))
            count += 1
        self.assertEqual(count, 2592)

    def test_null_without_applicable_weight_even_if_gross(self):
        # Editable custom checklist: weighted item N/A, zero-weight gross flag applies.
        checklist = {"name": "Synthetic custom edge", "provenance": "custom",
                     "criteria": [deepcopy(self.checklist["criteria"][1]), deepcopy(self.checklist["criteria"][7])],
                     "rules": {"decimals": 2, "gross_error_policy": "zero_total"}}
        answers = [deepcopy(self.answers[1]), deepcopy(self.answers[7])]
        answers[0].update(answer=None, na_reason="Synthetic condition")
        answers[1]["answer"] = True
        result = score(checklist, answers, self.conversation)
        self.assertIsNone(result["total"])
        self.assertTrue(result["gross_error"])
        self.assertEqual(result["applicable_weight"], 0)

    def test_custom_continuous_scale_and_preset_isolation(self):
        custom = deepcopy(self.checklist)
        custom["provenance"] = "custom"
        del custom["criteria"][3]["allowed_values"]
        self.answers[3]["answer"] = 12.5
        self.assertEqual(score(custom, self.answers, self.conversation)["criteria"][3]["normalized_score"], .625)
        custom["criteria"][0]["title"] = "Edited"
        self.assertEqual(official_checklist()["criteria"][0]["title"], "Установление контакта")

    def test_invalid_discrete_configuration(self):
        for values in [[], [0], [0, 5, 5, 20], [20, 0], [0, 5, 19], [True, 20], [0, float("nan"), 20]]:
            candidate = deepcopy(self.checklist)
            candidate["criteria"][3]["allowed_values"] = values
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_checklist(candidate)


if __name__ == "__main__":
    unittest.main()
