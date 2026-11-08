"""Synthetic examples, not the official checklist or organizer data."""
from copy import deepcopy
import os
import json
import unittest
from unittest.mock import MagicMock, patch

from server.evaluation import infer, score, validate_checklist


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.checklist = {"name": "демонстрационный пример, не официальный чек-лист", "provenance": "demonstration",
            "criteria": [{"id": "a", "title": "Example boolean", "type": "boolean", "weight": 1, "allow_na": True, "section": "one", "gross_error": True},
                         {"id": "b", "title": "Example scale", "type": "numeric", "min": 1, "max": 5, "weight": 3, "allow_na": True, "section": "two"}],
            "rules": {"decimals": 2, "gross_error_policy": "flag"}}
        self.conversation = {"transcript": [{"id": "demo:0", "speaker": "client"}, {"id": "demo:1", "speaker": "operator"}]}
        self.answers = [{"criterion_id": "a", "answer": True, "explanation": "Synthetic example", "evidence_message_ids": ["demo:1"]},
                        {"criterion_id": "b", "answer": 3, "explanation": "Synthetic example", "evidence_message_ids": ["demo:1"]}]

    def test_weighted_numeric_scale_and_sections(self):
        result = score(self.checklist, self.answers, self.conversation)
        self.assertEqual(result["total"], 62.5)
        self.assertEqual([x["total"] for x in result["sections"]], [100, 50])
        self.assertEqual(result["criteria"][1]["effective_weight"], .75)

    def test_na_redistribution_and_all_na(self):
        self.answers[1].update(answer=None, na_reason="Synthetic unavailable context")
        result = score(self.checklist, self.answers, self.conversation)
        self.assertEqual(result["total"], 100)
        self.assertEqual(result["criteria"][0]["effective_weight"], 1)
        self.answers[0].update(answer=None, na_reason="Synthetic unavailable context")
        self.assertIsNone(score(self.checklist, self.answers, self.conversation)["total"])

    def test_no_input_mutation(self):
        before = deepcopy((self.checklist, self.answers, self.conversation))
        score(self.checklist, self.answers, self.conversation)
        self.assertEqual(before, (self.checklist, self.answers, self.conversation))

    def test_invalid_numeric_and_schema(self):
        for value in [True, float("nan"), float("inf"), 0, 6, "3"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.answers[1]["answer"] = value
                score(self.checklist, self.answers, self.conversation)
        for field, value in [("weight", -1), ("weight", float("inf")), ("allow_na", "yes"), ("type", "string")]:
            candidate = deepcopy(self.checklist)
            candidate["criteria"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_checklist(candidate)

    def test_exact_coverage(self):
        for answers in [self.answers[:1], self.answers + [self.answers[0]], self.answers + [{"criterion_id": "unknown"}]]:
            with self.assertRaises(ValueError):
                score(self.checklist, answers, self.conversation)

    def test_evidence_and_missed_span(self):
        self.answers[0]["evidence_message_ids"] = ["absent"]
        with self.assertRaises(ValueError):
            score(self.checklist, self.answers, self.conversation)
        self.answers[0].update(answer=False, evidence_message_ids=[], missed=True)
        with self.assertRaises(ValueError):
            score(self.checklist, self.answers, self.conversation)
        self.answers[0]["checked_span"] = {"start_message_id": "demo:0", "end_message_id": "demo:1"}
        self.assertEqual(score(self.checklist, self.answers, self.conversation)["total"], 37.5)
        self.answers[0]["checked_span"] = {"start_message_id": "demo:1", "end_message_id": "demo:0"}
        with self.assertRaises(ValueError):
            score(self.checklist, self.answers, self.conversation)

    def test_na_reason_and_permission(self):
        self.answers[0]["answer"] = None
        with self.assertRaises(ValueError):
            score(self.checklist, self.answers, self.conversation)
        self.answers[0]["na_reason"] = "Example unavailable"
        self.checklist["criteria"][0]["allow_na"] = False
        with self.assertRaises(ValueError):
            score(self.checklist, self.answers, self.conversation)

    def test_gross_error_policy(self):
        self.answers[0]["gross_error"] = True
        self.assertEqual(score(self.checklist, self.answers, self.conversation)["total"], 62.5)
        self.checklist["rules"]["gross_error_policy"] = "zero_total"
        self.assertEqual(score(self.checklist, self.answers, self.conversation)["total"], 0)
        self.answers[1]["gross_error"] = True
        with self.assertRaises(ValueError):
            score(self.checklist, self.answers, self.conversation)

    def test_round_total_from_unrounded_values(self):
        self.checklist["criteria"][1]["max"] = 7
        self.answers[1]["answer"] = 2
        self.assertEqual(score(self.checklist, self.answers, self.conversation)["total"], 37.5)

    def test_half_up_and_zero_applicable_weight(self):
        self.checklist['criteria'][1].update(type='boolean', weight=31)
        self.answers[1]['answer'] = False
        self.assertEqual(score(self.checklist, self.answers, self.conversation)['total'], 3.13)
        self.checklist['criteria'][0]['weight'] = 0
        self.answers[1].update(answer=None, na_reason='Synthetic N/A')
        result = score(self.checklist, self.answers, self.conversation)
        self.assertIsNone(result['total'])
        self.assertEqual(result['applicable_weight'], 0)

    def test_unconfigured_and_external_provider(self):
        with patch.dict(os.environ, {"CQA_MODEL": ""}), self.assertRaisesRegex(ValueError, "модель не настроена"):
            infer(self.checklist, self.conversation)

    def test_provider_structured_validation_and_private_errors(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({"response": json.dumps({"answers": self.answers})}).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch.dict(os.environ, {"CQA_MODEL": "example", "CQA_OLLAMA_URL": "http://127.0.0.1:11434"}), patch("server.evaluation.build_opener", return_value=opener):
            self.assertEqual(infer(self.checklist, self.conversation), self.answers)
            payload = json.loads(opener.open.call_args.args[0].data)
            self.assertIn("ONLY the operator", payload["prompt"])
            # Local inference never inherits a shell/system HTTP proxy.
            # Such a proxy could receive transcript bodies despite loopback config.
            self.assertEqual(opener.open.call_args.args[0].host, '127.0.0.1:11434')
            response.read.return_value = b'{"response":"{\\"answers\\":[]}"}'
            with self.assertRaisesRegex(ValueError, "invalid structured evaluation"):
                infer(self.checklist, self.conversation)
            opener.open.side_effect = RuntimeError("private provider body")
            with self.assertRaises(ValueError) as error:
                infer(self.checklist, self.conversation)
            self.assertNotIn("private provider body", str(error.exception))
        with patch.dict(os.environ, {"CQA_MODEL": "example", "CQA_OLLAMA_URL": "https://example.com", "CQA_ALLOW_EXTERNAL_MODEL": "0"}), self.assertRaisesRegex(ValueError, "authorization"):
            infer(self.checklist, self.conversation)

    def test_local_provider_disables_environment_proxies_and_redirects(self):
        from urllib.request import ProxyHandler
        from server.evaluation import _NoRedirect
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({'response': json.dumps({'answers': self.answers})}).encode()
        with patch.dict(os.environ, {'CQA_MODEL': 'synthetic-test',
                                    'CQA_OLLAMA_URL': 'http://127.0.0.1:11434',
                                    'HTTP_PROXY': 'http://unrelated.invalid:3128',
                                    'ALL_PROXY': 'http://unrelated.invalid:3128'}), \
             patch('server.evaluation.build_opener') as builder:
            builder.return_value.open.return_value = response
            infer(self.checklist, self.conversation)
            handlers = builder.call_args.args
            self.assertTrue(any(isinstance(h, ProxyHandler) and h.proxies == {} for h in handlers))
            self.assertTrue(any(isinstance(h, _NoRedirect) for h in handlers))


if __name__ == "__main__":
    unittest.main()
