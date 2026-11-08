"""Inference-only source-proof checks using synthetic mocked transport."""
import copy
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from server.evaluation import infer, score
from server.presets import official_checklist
from server.semantic_check import synthetic_fixture


class LegacyInferenceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.checklist = official_checklist()
        self.conversation, _, _ = synthetic_fixture('EN', 'departure')
        self.answers = {
            c['id']: dict(answer=c['max'] if c['type'] == 'numeric' else c['id'] != '08',
                          explanation='Synthetic transport regression',
                          evidence_message_ids=['semantic-en-departure:1'],
                          missed=False, gross_error=False, checked_span=None, na_reason='')
            for c in self.checklist['criteria']}
        self.span = dict(start_message_id='semantic-en-departure:0',
                         end_message_id='semantic-en-departure:2')

    def call(self, answers, shape):
        output = (answers if shape == 'keyed' else
                  [{**a, 'criterion_id': ident} for ident, a in answers.items()])
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({
            'done': True, 'response': json.dumps({'answers': output})}).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch.dict(os.environ, {'CQA_MODEL': 'synthetic-transport-only', 'CQA_THINK': '1',
                                     'CQA_OLLAMA_URL': 'http://127.0.0.1:11434'}), \
                patch('server.evaluation.build_opener', return_value=opener):
            return infer(self.checklist, self.conversation)

    def test_combined_legacy_shapes_require_actual_operator_proof(self):
        for shape in ('keyed', 'list'):
            for role in ('client', 'bot', 'system'):
                for ident in ('01', '04', '08'):
                    answers = copy.deepcopy(self.answers)
                    self.conversation['transcript'][0]['speaker'] = role
                    answers[ident].update(answer=True if ident == '08' else answers[ident]['answer'],
                                          evidence_message_ids=['semantic-en-departure:0'])
                    with self.subTest(shape=shape, role=role, criterion=ident), \
                            self.assertRaisesRegex(ValueError, 'invalid structured evaluation'):
                        self.call(answers, shape)

    def test_na_missed_and_negative_gross_keep_valid_context_exemptions(self):
        for shape in ('keyed', 'list'):
            answers = copy.deepcopy(self.answers)
            answers['04'].update(answer=None, evidence_message_ids=['semantic-en-departure:2'],
                                 na_reason='Interrupted before request', na_condition_met=True)
            answers['07'].update(answer=False, evidence_message_ids=[], missed=True,
                                 checked_span=self.span)
            answers['08'].update(answer=False, evidence_message_ids=[], checked_span=self.span)
            with self.subTest(shape=shape):
                result = self.call(answers, shape)
                self.assertEqual(len(result), 8)
                self.assertNotIn('na_condition_met', result[3])

    def test_split_fields_in_list_cannot_mislabel_source_role(self):
        answers = copy.deepcopy(self.answers)
        del answers['01']['evidence_message_ids']
        answers['01'].update(operator_evidence_ids=['semantic-en-departure:0'], context_evidence_ids=[])
        with self.assertRaisesRegex(ValueError, 'invalid structured evaluation'):
            self.call(answers, 'list')

    def test_manual_score_contract_remains_source_preserving(self):
        # Historical/reviewer scoring remains separate from automatic inference.
        answers = copy.deepcopy(self.answers)
        answers['01']['evidence_message_ids'] = ['semantic-en-departure:0']
        rows = [{**a, 'criterion_id': ident} for ident, a in answers.items()]
        result = score(self.checklist, rows, self.conversation)
        self.assertEqual(result['criteria'][0]['evidence_message_ids'], ['semantic-en-departure:0'])


if __name__ == '__main__':
    unittest.main()
