"""Audit-runner tests use explicitly mocked answers, never real inference."""
import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from server.semantic_check import LANGUAGES, SCENARIOS, check_semantics, main, synthetic_fixture
from server.presets import official_checklist
from server.evaluation import PROMPT_REVISION


class SemanticCheckTests(unittest.TestCase):
    def test_audit_snapshots_provider_configuration_without_endpoint_or_thinking_trace(self):
        config = {"CQA_MODEL": " qwen3:8b ", "CQA_THINK": "1",
                  "CQA_OLLAMA_URL": "http://PRIVATE_ENDPOINT", "CQA_API_KEY": "PRIVATE_KEY"}

        def changing_config(checklist, conversation):
            os.environ["CQA_MODEL"] = "changed-after-start"
            os.environ["CQA_THINK"] = "0"
            return self.fixture_answers(checklist, conversation)

        with patch.dict(os.environ, config), patch("server.semantic_check.infer", side_effect=changing_config):
            report = check_semantics(["EN"], ["completed"])
        self.assertEqual(report["provider"], {"type": "ollama", "model": "qwen3:8b",
                                            "prompt_revision": PROMPT_REVISION, "think": True})
        self.assertNotIn("PRIVATE_", json.dumps(report))
        self.assertEqual(report["status"], "passed")

    def test_failed_provider_audit_retains_provenance(self):
        with patch.dict(os.environ, {"CQA_MODEL": "candidate", "CQA_THINK": "1"}), \
                patch("server.semantic_check.infer", side_effect=ValueError("PRIVATE_BODY")):
            report = check_semantics(["EN"], ["completed"])
        self.assertEqual(report["provider"]["model"], "candidate")
        self.assertTrue(report["provider"]["think"])
        self.assertEqual(report["provider"]["prompt_revision"], PROMPT_REVISION)
        self.assertEqual(report["status"], "failed")
        self.assertNotIn("PRIVATE_BODY", json.dumps(report))

    @staticmethod
    def fixture_answers(checklist, conversation):
        scenario = conversation["conversation_id"].split("-")[-1]
        _, expected, evidence = synthetic_fixture(conversation["language"], scenario)
        messages = conversation["transcript"]
        answers = []
        for criterion in checklist["criteria"]:
            ident = criterion["id"]
            value = expected.get(ident, criterion.get("max", True))
            answers.append({"criterion_id": ident, "answer": value,
                            "explanation": "TEST ONLY: mocked semantic fixture answer",
                            "evidence_message_ids": evidence.get(ident, [messages[1]["id"]]),
                            "checked_span": {"start_message_id": messages[0]["id"],
                                             "end_message_id": messages[-1]["id"]},
                            "na_reason": "TEST ONLY: fixture applicability" if value is None else ""})
        return answers

    def test_all_nine_fixtures_have_independent_semantic_and_evidence_gates(self):
        with patch("server.semantic_check.infer", side_effect=self.fixture_answers) as provider:
            report = check_semantics()
        self.assertEqual(provider.call_count, 9)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["structural_status"], "passed")
        self.assertEqual(report["semantic_status"], "passed")
        self.assertEqual({(r["language"], r["scenario"]) for r in report["checks"]},
                         {(language, scenario) for language in LANGUAGES for scenario in SCENARIOS})
        self.assertTrue(report["synthetic_only"])

    def test_bot_greeting_cannot_satisfy_operator_greeting(self):
        def wrong(checklist, conversation):
            answers = self.fixture_answers(checklist, conversation)
            answers[0].update(answer=True, evidence_message_ids=[conversation["transcript"][1]["id"]])
            return answers
        with patch("server.semantic_check.infer", side_effect=wrong):
            report = check_semantics(LANGUAGES, ["bot_only_greeting"])
        self.assertEqual(report["structural_status"], "passed")
        self.assertEqual(report["semantic_status"], "failed")
        for check in report["checks"]:
            self.assertIn({"criterion_id": "01", "check": "operator_evidence", "status": "failed"},
                          check["assertions"])

    def test_missed_greeting_accepts_checked_operator_span_without_quote(self):
        def missing(checklist, conversation):
            answers = self.fixture_answers(checklist, conversation)
            operator_id = conversation["transcript"][2]["id"]
            answers[0].update(missed=True, evidence_message_ids=[],
                              checked_span={"start_message_id": operator_id, "end_message_id": operator_id})
            return answers
        with patch("server.semantic_check.infer", side_effect=missing):
            report = check_semantics(LANGUAGES, ["bot_only_greeting"])
        self.assertEqual(report["status"], "passed")

    def test_missed_greeting_bot_only_span_is_not_operator_evidence(self):
        def missing(checklist, conversation):
            answers = self.fixture_answers(checklist, conversation)
            bot_id = conversation["transcript"][1]["id"]
            answers[0].update(missed=True, evidence_message_ids=[],
                              checked_span={"start_message_id": bot_id, "end_message_id": bot_id})
            return answers
        with patch("server.semantic_check.infer", side_effect=missing):
            report = check_semantics(["EN"], ["bot_only_greeting"])
        self.assertEqual(report["structural_status"], "passed")
        self.assertEqual(report["semantic_status"], "failed")

    def test_structurally_valid_wrong_grade_fails_semantic_gate(self):
        def wrong(checklist, conversation):
            answers = self.fixture_answers(checklist, conversation)
            answers[0]["answer"] = False
            return answers
        with patch("server.semantic_check.infer", side_effect=wrong):
            report = check_semantics(["RU"], ["completed"])
        self.assertEqual(report["structural_status"], "passed")
        self.assertEqual(report["semantic_status"], "failed")
        self.assertEqual(report["status"], "failed")

    def test_valid_grade_with_client_evidence_fails_semantic_gate(self):
        def wrong(checklist, conversation):
            answers = self.fixture_answers(checklist, conversation)
            answers[0]["evidence_message_ids"] = [conversation["transcript"][0]["id"]]
            return answers
        with patch("server.semantic_check.infer", side_effect=wrong):
            report = check_semantics(["KZ"], ["completed"])
        check = report["checks"][0]
        self.assertEqual(check["structural_status"], "passed")
        self.assertIn({"criterion_id": "01", "check": "operator_evidence", "status": "failed"},
                      check["assertions"])

    def test_na_in_completed_closing_is_semantic_failure(self):
        def wrong(checklist, conversation):
            answers = self.fixture_answers(checklist, conversation)
            answers[6].update(answer=None, na_reason="TEST ONLY: incorrect N/A")
            return answers
        with patch("server.semantic_check.infer", side_effect=wrong):
            report = check_semantics(["EN"], ["completed"])
        self.assertEqual(report["structural_status"], "passed")
        self.assertEqual(report["semantic_status"], "failed")

    def test_bool_int_confusion_never_passes_the_gate(self):
        # Through the real scorer, type confusion is a structural rejection, never a pass.
        for index, value in ((0, 1), (3, True)):  # 01 boolean given int, 04 numeric given bool
            def confused(checklist, conversation, index=index, value=value):
                answers = self.fixture_answers(checklist, conversation)
                answers[index]["answer"] = value
                return answers
            with self.subTest(index=index, value=value), \
                    patch("server.semantic_check.infer", side_effect=confused):
                report = check_semantics(["EN"], ["completed"])
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["structural_status"], "failed")

    def test_semantic_comparison_itself_rejects_bool_int_confusion(self):
        # Defense in depth: even if a scorer let 1/True through, the semantic gate must fail.
        from server.evaluation import score as real_score

        def lenient(checklist, answers, conversation):
            result = real_score(checklist, answers, conversation)
            for row in result["criteria"]:
                if row["criterion_id"] == "01":
                    row["answer"] = 1
                if row["criterion_id"] == "04":
                    row["answer"] = True
            return result
        with patch("server.semantic_check.infer", side_effect=self.fixture_answers), \
                patch("server.semantic_check.score", side_effect=lenient):
            report = check_semantics(["RU"], ["completed"])
        self.assertEqual(report["structural_status"], "passed")
        self.assertEqual(report["semantic_status"], "failed")
        failed = {(a["criterion_id"], a["check"]) for a in report["checks"][0]["assertions"]
                  if a["status"] == "failed"}
        self.assertEqual(failed, {("01", "answer"), ("04", "answer")})

    def test_no_instead_of_na_for_sufficient_initial_data_fails(self):
        def wrong(checklist, conversation):
            answers = self.fixture_answers(checklist, conversation)
            answers[1].update(answer=False, na_reason="")  # 02 expected N/A, not "No"
            return answers
        with patch("server.semantic_check.infer", side_effect=wrong):
            report = check_semantics(["KZ"], ["completed"])
        self.assertEqual(report["structural_status"], "passed")
        self.assertEqual(report["semantic_status"], "failed")

    def test_invalid_answers_fail_structure_before_semantic_checks(self):
        with patch("server.semantic_check.infer", return_value=[]):
            report = check_semantics(["EN"])
        self.assertEqual(report["structural_status"], "failed")
        self.assertEqual(report["semantic_status"], "not_run")

    def test_provider_errors_fail_fast_and_never_expose_private_details(self):
        with patch("server.semantic_check.infer", side_effect=RuntimeError("PRIVATE_PROVIDER_BODY")) as provider:
            report = check_semantics()
        self.assertEqual(provider.call_count, 1)
        self.assertNotIn("PRIVATE_PROVIDER_BODY", json.dumps(report))
        self.assertEqual(report["checks"][0]["failure"], "provider_or_structure_failed")

    def test_missing_model_is_explicit(self):
        with patch("server.semantic_check.infer", side_effect=ValueError("модель не настроена")):
            report = check_semantics()
        self.assertEqual(report["checks"][0]["failure"], "model_not_configured")

    def test_output_contains_no_transcripts_or_model_explanations(self):
        with patch("server.semantic_check.infer", side_effect=self.fixture_answers):
            report = json.dumps(check_semantics(), ensure_ascii=False)
        for language in LANGUAGES:
            for scenario in SCENARIOS:
                conversation, _, _ = synthetic_fixture(language, scenario)
                for message in conversation["transcript"]:
                    self.assertNotIn(message["text"], report)
        self.assertNotIn("mocked semantic fixture", report)

    def test_cli_selects_fixture_and_exits_nonzero_on_semantic_failure(self):
        answers = self.fixture_answers(official_checklist(), synthetic_fixture("EN", "completed")[0])
        answers[0]["answer"] = False
        with patch("server.semantic_check.infer", return_value=answers), contextlib.redirect_stdout(io.StringIO()) as out:
            exit_code = main(["--language", "EN", "--scenario", "completed"])
        self.assertEqual(exit_code, 1)
        report = json.loads(out.getvalue())
        self.assertEqual(len(report["checks"]), 1)
        self.assertEqual(report["structural_status"], "passed")
        self.assertEqual(report["semantic_status"], "failed")


if __name__ == "__main__":
    unittest.main()
