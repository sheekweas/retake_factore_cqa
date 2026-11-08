"""Transport regressions for source roles and legitimate system-context N/A.

These are labelled synthetic responses, not real-model acceptance results.
"""
import copy
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from server.evaluation import infer, score
from server.presets import official_checklist
from server.semantic_check import synthetic_fixture


class SourceEvidenceAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.checklist = official_checklist()
        self.conversation, _, _ = synthetic_fixture('EN', 'departure')
        self.answers = {}
        for criterion in self.checklist['criteria']:
            value = criterion['max'] if criterion['type'] == 'numeric' else criterion['id'] != '08'
            self.answers[criterion['id']] = dict(
                answer=value, explanation='Synthetic role-validation regression',
                operator_evidence_ids=['semantic-en-departure:1'], context_evidence_ids=[],
                missed=False, gross_error=False, checked_span=None, na_reason='')

    def infer_response(self, answers):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({
            'done': True, 'response': json.dumps({'answers': answers})}).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch.dict(os.environ, {'CQA_MODEL': 'synthetic-transport-only', 'CQA_THINK': '1'}), \
                patch('server.evaluation.build_opener', return_value=opener):
            return infer(self.checklist, self.conversation)

    def test_all_context_roles_cannot_be_relabeled_as_operator_proof(self):
        # The source role, rather than text or a provider label, is authoritative.
        for role in ('client', 'bot', 'system'):
            self.conversation['transcript'][0]['speaker'] = role
            answers = copy.deepcopy(self.answers)
            answers['01']['operator_evidence_ids'] = ['semantic-en-departure:0']
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, 'invalid structured evaluation'):
                self.infer_response(answers)

    def test_positive_gross_error_cannot_use_only_system_context(self):
        answers = copy.deepcopy(self.answers)
        answers['08'].update(answer=True, operator_evidence_ids=[],
                             context_evidence_ids=['semantic-en-departure:2'])
        with self.assertRaisesRegex(ValueError, 'invalid structured evaluation'):
            self.infer_response(answers)

    def test_departure_na_can_be_supported_by_actual_system_message(self):
        for ident in ('04', '07'):
            self.answers[ident].update(
                answer=None, operator_evidence_ids=[],
                context_evidence_ids=['semantic-en-departure:2'], na_condition_met=True,
                na_reason='Synthetic system event explicitly records interruption before request and closing opportunity.')
        result = self.infer_response(self.answers)
        rows = {row['criterion_id']: row for row in score(self.checklist, result, self.conversation)['criteria']}
        for ident in ('04', '07'):
            self.assertIsNone(rows[ident]['answer'])
            self.assertEqual(rows[ident]['evidence_message_ids'], ['semantic-en-departure:2'])
        self.assertEqual(self.conversation['transcript'][2]['speaker'], 'system')


if __name__ == '__main__':
    unittest.main()
