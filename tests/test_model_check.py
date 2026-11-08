"""Smoke-runner regression tests, synthetic responses only, not real inference."""
import json
import unittest
from unittest.mock import patch

from server.model_check import check_model, synthetic_conversation


class ModelCheckTests(unittest.TestCase):
    @staticmethod
    def structured_answers(checklist, conversation):
        return [{"criterion_id": c["id"],
                 "answer": False if c["id"] == "08" else c.get("max", True),
                 "explanation": "TEST ONLY: synthetic structured response",
                 "evidence_message_ids": [conversation["transcript"][1]["id"]]}
                for c in checklist["criteria"]]

    def test_three_languages_use_real_adapter_boundary_and_official_checklist(self):
        with patch("server.model_check.infer", side_effect=self.structured_answers) as provider:
            report = check_model(["RU", "KZ", "EN"])
        self.assertEqual(provider.call_count, 3)
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["synthetic_only"])
        self.assertEqual([r["language"] for r in report["checks"]], ["RU", "KZ", "EN"])
        for check in report["checks"]:
            self.assertEqual(check["criterion_count"], 8)
            self.assertEqual(check["total"], 100)
        for call in provider.call_args_list:
            checklist, conversation = call.args
            self.assertEqual(checklist["provenance"], "official")
            self.assertEqual(conversation["dataset_group_id"], "SYNTHETIC_MODEL_CHECK")
            self.assertEqual([m["speaker"] for m in conversation["transcript"]], ["client", "operator"])
            self.assertTrue(conversation["label"])

    def test_provider_error_fails_fast_and_redacts_private_details(self):
        with patch("server.model_check.infer", side_effect=RuntimeError("PRIVATE_URL_AND_BODY")) as provider:
            report = check_model(["RU", "KZ", "EN"])
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(report["checks"]), 1)
        self.assertNotIn("PRIVATE_URL_AND_BODY", json.dumps(report))

    def test_invalid_structured_response_cannot_pass(self):
        with patch("server.model_check.infer", return_value=[]):
            self.assertEqual(check_model(["KZ"])["status"], "failed")

    def test_unconfigured_model_reports_honest_reason(self):
        with patch("server.model_check.infer", side_effect=ValueError("модель не настроена")):
            report = check_model(["RU"])
        self.assertEqual(report["status"], "failed")
        self.assertIn("модель не настроена", report["message"])

    def test_reports_contain_no_transcript_text(self):
        with patch("server.model_check.infer", side_effect=self.structured_answers):
            report = json.dumps(check_model(["EN"]))
        for message in synthetic_conversation("EN")["transcript"]:
            self.assertNotIn(message["text"], report)


if __name__ == "__main__":
    unittest.main()
