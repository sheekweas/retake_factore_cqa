"""Timeout recovery and added synthetic closing boundary; no real inference."""
import os
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import URLError
from server.evaluation import infer
from server.presets import official_checklist
from server.semantic_check import SCENARIOS, check_semantics, synthetic_fixture

class timeoutProviderTests(unittest.TestCase):
    def test_default_timeout_600_and_private_timeout_is_retryable(self):
        c, _, _ = synthetic_fixture("EN", "completed")
        for failure in (TimeoutError("PRIVATE"), URLError(TimeoutError("PRIVATE"))):
            opener = MagicMock()
            opener.open.side_effect = failure
            with self.subTest(failure=type(failure).__name__), patch.dict(os.environ, {"CQA_MODEL":"test-only"}, clear=True), patch("server.evaluation.build_opener", return_value=opener):
                with self.assertRaisesRegex(ValueError, "Повторите оценку") as error:
                    infer(official_checklist(), c)
                self.assertNotIn("PRIVATE", str(error.exception))
                self.assertEqual(opener.open.call_args.kwargs["timeout"], 600)

    def fixture_answers(self, checklist, conversation):
        scenario = conversation["conversation_id"].split("-")[-1]
        _, expected, evidence = synthetic_fixture(conversation["language"], scenario)
        messages = conversation["transcript"]
        return [{"criterion_id": c["id"], "answer":expected[c["id"]],
                 "explanation":"TEST ONLY synthetic answer", "evidence_message_ids":evidence.get(c["id"],[messages[1]["id"]]),
                 "na_reason":"TEST ONLY applicability" if expected[c["id"]] is None else ""}
                for c in checklist["criteria"]]

    def test_farewell_is_opt_in_and_original_gate_unchanged(self):
        self.assertEqual(SCENARIOS, ("completed", "departure", "bot_only_greeting"))
        for language in ("RU", "KZ", "EN"):
            c, expected, evidence = synthetic_fixture(language, "farewell_only")
            self.assertIn("синтетический", c["label"])
            self.assertIs(expected["07"], False)
            self.assertIs(expected["01"], True)
            self.assertEqual(expected["06"], 10)
            self.assertEqual(evidence["07"], [c["transcript"][3]["id"]])

    def test_farewell_only_false_passes_yes_fails(self):
        with patch("server.semantic_check.infer", side_effect=self.fixture_answers):
            self.assertEqual(check_semantics(["EN"], ["farewell_only"])["status"], "passed")
        def wrong(checklist, conversation):
            answers = self.fixture_answers(checklist, conversation)
            answers[6]["answer"] = True
            return answers
        with patch("server.semantic_check.infer", side_effect=wrong):
            report = check_semantics(["EN"], ["farewell_only"])
        self.assertEqual(report["structural_status"], "passed")
        self.assertEqual(report["semantic_status"], "failed")
