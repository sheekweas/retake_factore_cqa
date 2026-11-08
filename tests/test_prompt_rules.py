"""rubric prompt regressions (operator-evidence-v7); mocked transport only, no real model."""
import copy
import json
import os
import unittest
from unittest.mock import MagicMock, patch

import server.semantic_check
from server.evaluation import PROMPT_REVISION, _response_schema, infer, score, validate_checklist
from server.model_check import synthetic_conversation
from server.presets import official_checklist

CLIENT, OPERATOR = 'model-check-ru:0', 'model-check-ru:1'


class Stage14PromptTests(unittest.TestCase):
    def setUp(self):
        self.checklist = official_checklist()
        self.conversation = synthetic_conversation('RU')
        self.answers = {}
        for criterion in self.checklist['criteria']:
            value = criterion['max'] if criterion['type'] == 'numeric' else criterion['id'] != '08'
            entry = dict(answer=value, explanation='Synthetic rubric regression',
                         operator_evidence_ids=[OPERATOR], context_evidence_ids=[],
                         missed=False, gross_error=False, checked_span=None, na_reason='')
            if criterion['allow_na']:
                entry['na_condition_met'] = False
            if criterion['id'] == '08':
                entry.update(operator_evidence_ids=[],
                             checked_span={'start_message_id': CLIENT, 'end_message_id': OPERATOR})
            self.answers[criterion['id']] = entry

    def call(self, answers):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {'done': True, 'done_reason': 'stop', 'response': json.dumps({'answers': answers})}).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch.dict(os.environ, {'CQA_MODEL': 'synthetic-transport-only',
                                     'CQA_OLLAMA_URL': 'http://127.0.0.1:11434', 'CQA_THINK': '0'}), \
                patch('server.evaluation.build_opener', return_value=opener):
            result = infer(self.checklist, self.conversation)
        return result, json.loads(opener.open.call_args.args[0].data)

    def set_na(self, answers, ident):
        answers[ident].update(answer=None, na_condition_met=True, operator_evidence_ids=[],
                              context_evidence_ids=[CLIENT],
                              na_reason='Chat ended before a substantive request / closing opportunity.')

    def set_farewell_only(self, answers):
        answers['07'].update(answer=False, missed=True,
                             checked_span={'start_message_id': CLIENT, 'end_message_id': OPERATOR},
                             explanation='Farewell only: no summary and no offer of further help.')

    def rows(self, result):
        report = score(self.checklist, result, self.conversation)
        return report, {r['criterion_id']: r for r in report['criteria']}

    def test_prompt_revision_and_v5_rules_are_sent(self):
        self.assertEqual(PROMPT_REVISION, 'operator-evidence-v7')
        _, payload = self.call(self.answers)
        prompt = payload['prompt']
        # (a) consultation applicability: routing/greeting is not a request; N/A, never 0.
        for phrase in ('a greeting, menu choice, bot routing or a transfer', 'is not yet a request',
                       'consultation N/A condition holds: return null', 'never 0'):
            self.assertIn(phrase, prompt)
        # (b) completion: every 'and' part must be in operator messages; farewell alone is No.
        for phrase in ("every part must appear in OPERATOR messages", 'a farewell alone is',
                       'ALSO summarised the result or offered further help'):
            self.assertIn(phrase, prompt)
        # (c) excluded checks are not mentioned or applied; timing only as an explicit wait.
        for phrase in ('first-response time threshold, tags or attachments',
                       'do not mention', 'explicit waiting period',
                       'not only a later recap or farewell',
                       'if no such words exist, answer No', 'must not contain minutes'):
            self.assertIn(phrase, prompt)

    def test_schema_allows_na_for_04_07_and_missed_for_07(self):
        schema = _response_schema(validate_checklist(self.checklist), self.conversation)
        props = schema['properties']['answers']['properties']
        for ident in ('04', '07'):
            na = props[ident]['anyOf'][-1]['properties']
            self.assertEqual(na['answer'], {'type': 'null'})
            self.assertEqual(na['na_condition_met'], {'const': True})
            self.assertEqual(na['na_reason']['minLength'], 1)
        missing = [b['properties'] for b in props['07']['anyOf'] if b['properties']['missed'] == {'const': True}]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]['answer'], {'const': False})
        self.assertIn('start_message_id', missing[0]['checked_span']['properties'])

    def test_na_for_04_and_07_scores_over_applicable_weights(self):
        answers = copy.deepcopy(self.answers)
        for ident in ('04', '07'):
            self.set_na(answers, ident)
        report, rows = self.rows(self.call(answers)[0])
        for ident in ('04', '07'):
            self.assertIsNone(rows[ident]['answer'])
            self.assertEqual(rows[ident]['weighted_score'], 0.0)
        self.assertEqual(report['applicable_weight'], 75.0)
        self.assertEqual(report['total'], 100.0)

    def test_farewell_only_07_is_missed_false_and_contributes_zero(self):
        answers = copy.deepcopy(self.answers)
        self.set_farewell_only(answers)
        report, rows = self.rows(self.call(answers)[0])
        self.assertIs(rows['07']['answer'], False)
        self.assertTrue(rows['07']['missed'])
        self.assertEqual(rows['07']['weighted_score'], 0.0)
        self.assertEqual(report['applicable_weight'], 100.0)
        self.assertEqual(report['total'], 95.0)

    def test_04_na_with_07_farewell_only_total_over_applicable(self):
        answers = copy.deepcopy(self.answers)
        self.set_na(answers, '04')
        self.set_farewell_only(answers)
        report, rows = self.rows(self.call(answers)[0])
        self.assertIsNone(rows['04']['answer'])
        self.assertEqual(rows['07']['weighted_score'], 0.0)
        self.assertEqual(report['applicable_weight'], 80.0)
        self.assertEqual(report['total'], 93.75)

    def test_null_04_without_na_condition_met_is_rejected(self):
        answers = copy.deepcopy(self.answers)
        self.set_na(answers, '04')
        answers['04']['na_condition_met'] = False
        with self.assertRaisesRegex(ValueError, 'invalid structured evaluation'):
            self.call(answers)

    def test_semantic_check_provenance_uses_v5(self):
        self.assertEqual(server.semantic_check.PROMPT_REVISION, 'operator-evidence-v7')


if __name__ == '__main__':
    unittest.main()
