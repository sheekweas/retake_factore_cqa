"""Bounded acceptance-run and configuration safety checks; no real inference."""
import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from server.evaluation import PROMPT_REVISION
from server.semantic_check import main, synthetic_fixture


class Stage11AuditTests(unittest.TestCase):
    def test_en_cli_runs_exactly_three_selected_cases_and_preserves_provenance(self):
        seen = []

        def labelled_answers(checklist, conversation):
            scenario = conversation["conversation_id"].removeprefix("semantic-en-")
            seen.append((conversation["language"], scenario))
            _, expected, evidence = synthetic_fixture("EN", scenario)
            messages = conversation["transcript"]
            return [{"criterion_id": criterion["id"],
                     "answer": expected.get(criterion["id"], criterion.get("max", True)),
                     "explanation": "TEST ONLY: labelled audit response",
                     "evidence_message_ids": evidence.get(criterion["id"], [messages[1]["id"]]),
                     "checked_span": {"start_message_id": messages[0]["id"],
                                      "end_message_id": messages[-1]["id"]},
                     "na_reason": "TEST ONLY: applicability" if expected.get(criterion["id"], True) is None else ""}
                    for criterion in checklist["criteria"]]

        with patch.dict(os.environ, {"CQA_MODEL": "qwen3:8b", "CQA_THINK": "1"}), \
                patch("server.semantic_check.infer", side_effect=labelled_answers), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            exit_code = main(["--language", "EN"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(seen, [("EN", "completed"), ("EN", "departure"), ("EN", "bot_only_greeting")])
        report = json.loads(output.getvalue())
        self.assertEqual(report["provider"], {"type": "ollama", "model": "qwen3:8b",
                                             "think": True, "prompt_revision": PROMPT_REVISION})
        self.assertEqual(len(report["checks"]), 3)
        self.assertEqual(report["semantic_status"], "passed")

    def test_invalid_thinking_configuration_fails_before_any_transport_and_stays_safe(self):
        with patch.dict(os.environ, {"CQA_MODEL": "qwen3:8b", "CQA_THINK": "PRIVATE_INVALID",
                                    "CQA_OLLAMA_URL": "http://127.0.0.1:11434"}), \
                patch("server.evaluation.build_opener") as transport, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            exit_code = main(["--language", "EN"])
        transport.assert_not_called()
        report = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertIsNone(report["provider"]["think"])
        self.assertEqual(report["provider"]["prompt_revision"], PROMPT_REVISION)
        self.assertEqual(report["structural_status"], "failed")
        self.assertEqual(report["semantic_status"], "not_run")
        self.assertEqual(len(report["checks"]), 1)
        self.assertNotIn("PRIVATE_INVALID", output.getvalue())


if __name__ == "__main__":
    unittest.main()
