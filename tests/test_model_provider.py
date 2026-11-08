"""Provider protocol regressions; fake transport only, never fake production inference."""
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from server.evaluation import _response_schema, infer, score, validate_checklist
from server.model_check import synthetic_conversation
from server.presets import official_checklist


class ModelProviderTests(unittest.TestCase):
    def setUp(self):
        self.checklist = official_checklist()
        self.conversation = synthetic_conversation('RU')
        self.answers = {}
        for criterion in self.checklist['criteria']:
            value = criterion['max'] if criterion['type'] == 'numeric' else criterion['id'] != '08'
            self.answers[criterion['id']] = dict(
                answer=value, explanation='Synthetic transport regression',
                evidence_message_ids=['model-check-ru:1'], missed=False,
                gross_error=False, checked_span=None, na_reason='')

    def call_provider(self, content, **envelope):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(dict(response=json.dumps(content), **envelope)).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch.dict(os.environ, {'CQA_MODEL': 'synthetic-protocol-test',
                                     'CQA_OLLAMA_URL': 'http://127.0.0.1:11434',
                                     'CQA_NUM_CTX': '16384', 'CQA_MODEL_TIMEOUT': '300', 'CQA_THINK': '0'}), \
             patch('server.evaluation.build_opener', return_value=opener):
            result = infer(self.checklist, self.conversation)
        return result, json.loads(opener.open.call_args.args[0].data)

    def test_keyed_schema_preserves_ids_steps_na_and_evidence(self):
        schema = _response_schema(validate_checklist(self.checklist), self.conversation)
        answers = schema['properties']['answers']
        self.assertEqual(answers['required'], ['01', '02', '03', '04', '05', '06', '07', '08'])
        self.assertFalse(answers['additionalProperties'])
        fields = {key: value['anyOf'][0]['properties'] for key, value in answers['properties'].items() if key != '08'}
        self.assertEqual(fields['01']['answer'], {'type': 'boolean'})
        self.assertEqual(list(fields['01'])[:4], ['explanation', 'operator_evidence_ids', 'context_evidence_ids', 'answer'])
        self.assertEqual(answers['properties']['02']['anyOf'][-1]['properties']['answer'], {'type': 'null'})
        self.assertEqual(answers['properties']['02']['anyOf'][-1]['properties']['missed'], {'const': False})
        self.assertEqual(answers['properties']['02']['anyOf'][-1]['properties']['na_reason']['minLength'], 1)
        self.assertEqual(fields['04']['answer']['enum'], [0, 5, 10, 15, 20])
        self.assertEqual(fields['06']['answer']['enum'], [0, 4, 7, 10])
        self.assertEqual(fields['01']['gross_error'], {'const': False})
        negative, positive = answers['properties']['08']['anyOf']
        self.assertEqual(negative['properties']['answer'], {'const': False})
        self.assertEqual(negative['properties']['missed'], {'const': False})
        self.assertEqual(negative['properties']['checked_span']['properties']['start_message_id'],
                         {'const': 'model-check-ru:0'})
        self.assertEqual(negative['properties']['checked_span']['properties']['end_message_id'],
                         {'const': 'model-check-ru:1'})
        self.assertEqual(positive['properties']['answer'], {'const': True})
        self.assertEqual(positive['properties']['operator_evidence_ids']['minItems'], 1)
        # Operator proof and client/bot/system context are separate enums.
        self.assertEqual(fields['01']['operator_evidence_ids']['items']['enum'], ['model-check-ru:1'])
        self.assertEqual(fields['01']['context_evidence_ids']['items']['enum'], ['model-check-ru:0'])
        self.assertNotIn('minItems', fields['01']['context_evidence_ids'])

    def test_constrained_keyed_answer_normalized_and_context_explicit(self):
        result, payload = self.call_provider({'answers': self.answers}, done=True, done_reason='stop')
        self.assertEqual([a['criterion_id'] for a in result], list(self.answers))
        self.assertEqual(payload['format']['type'], 'object')
        self.assertEqual(payload['options']['num_ctx'], 16384)
        self.assertEqual(payload['options']['temperature'], 0)
        self.assertEqual(payload['options']['num_predict'], 8192)
        self.assertIs(payload['think'], False)
        self.assertIn('one property per criterion ID', payload['prompt'])

    def test_configured_output_bound_reaches_real_transport_options(self):
        with patch.dict(os.environ, {'CQA_NUM_PREDICT': '4096'}):
            _, payload = self.call_provider({'answers': self.answers}, done=True, done_reason='stop')
        self.assertEqual(payload['options']['num_predict'], 4096)

    def test_invalid_output_bounds_are_rejected_before_inference(self):
        for value in ('0', '-1', '32769', '1.5', 'unlimited'):
            with self.subTest(value=value), patch.dict(os.environ, {'CQA_NUM_PREDICT': value}):
                with self.assertRaisesRegex(ValueError, 'invalid CQA_NUM_PREDICT'):
                    self.call_provider({'answers': self.answers})

    def test_separate_provider_thinking_is_never_returned_as_evidence(self):
        result, payload = self.call_provider(
            {'answers': self.answers}, done=True, thinking='PRIVATE_REASONING_SENTINEL')
        self.assertIs(payload['think'], False)
        self.assertNotIn('PRIVATE_REASONING_SENTINEL', json.dumps(result))
        self.assertEqual([a['criterion_id'] for a in result], list(self.answers))

    def test_inline_reasoning_cannot_bypass_structured_json_validation(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({
            'done': True,
            'response': '<think>PRIVATE_REASONING_SENTINEL</think>' + json.dumps({'answers': self.answers}),
        }).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch.dict(os.environ, {'CQA_MODEL': 'synthetic-protocol-test'}), \
             patch('server.evaluation.build_opener', return_value=opener), \
             self.assertRaisesRegex(ValueError, '^model request failed or returned invalid structured evaluation$'):
            infer(self.checklist, self.conversation)

    def test_missing_or_unknown_key_rejected(self):
        for key in ('08', 'unknown'):
            answers = dict(self.answers)
            if key == '08':
                del answers[key]
            else:
                answers[key] = answers['01']
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'invalid structured evaluation'):
                self.call_provider({'answers': answers})

    def test_schema_is_not_substitute_for_server_validation(self):
        self.answers['06']['answer'] = 8
        with self.assertRaisesRegex(ValueError, 'invalid structured evaluation'):
            self.call_provider({'answers': self.answers})
        self.answers['06']['answer'] = 7
        self.answers['01']['evidence_message_ids'] = ['invented-id']
        with self.assertRaisesRegex(ValueError, 'invalid structured evaluation'):
            self.call_provider({'answers': self.answers})


    def score_answers(self):
        return score(self.checklist, [{**a, 'criterion_id': key} for key, a in self.answers.items()],
                     self.conversation)

    def test_absent_gross_error_requires_actual_full_span_when_no_quote(self):
        finding = self.answers['08']
        finding['evidence_message_ids'] = []
        with self.assertRaisesRegex(ValueError, 'requires evidence'):
            self.score_answers()
        finding['checked_span'] = {'start_message_id': 'model-check-ru:1', 'end_message_id': 'model-check-ru:1'}
        with self.assertRaisesRegex(ValueError, 'requires evidence'):
            self.score_answers()
        finding['checked_span']['start_message_id'] = 'model-check-ru:0'
        result = self.score_answers()
        self.assertFalse(result['gross_error'])
        self.assertEqual(result['total'], 100)
        self.assertFalse(result['criteria'][-1]['missed'])
        finding['missed'] = True
        with self.assertRaisesRegex(ValueError, 'not a missed required action'):
            self.score_answers()

    def test_full_span_does_not_replace_quote_for_positive_gross_or_other_answer(self):
        self.answers['08'].update(answer=True, evidence_message_ids=[], checked_span={
            'start_message_id': 'model-check-ru:0', 'end_message_id': 'model-check-ru:1'})
        with self.assertRaisesRegex(ValueError, 'requires evidence'):
            self.score_answers()
        self.answers['08']['evidence_message_ids'] = ['model-check-ru:1']
        self.answers['05'].update(answer=False, evidence_message_ids=[], checked_span={
            'start_message_id': 'model-check-ru:0', 'end_message_id': 'model-check-ru:1'})
        with self.assertRaisesRegex(ValueError, 'requires evidence'):
            self.score_answers()



    def test_schema_all_applicable_branches_require_quote_or_real_missing_span(self):
        schema = _response_schema(validate_checklist(self.checklist), self.conversation)
        criteria = schema['properties']['answers']['properties']
        for key in ['01', '02', '03', '04', '05', '06', '07']:
            branches = criteria[key]['anyOf']
            with self.subTest(criterion=key):
                quoted = branches[0]['properties']
                self.assertEqual(quoted['operator_evidence_ids']['minItems'], 1)
                self.assertEqual(quoted['missed'], {'const': False})
                missing = branches[1]['properties']
                self.assertEqual(missing['missed'], {'const': True})
                self.assertEqual(missing['checked_span']['type'], 'object')
                if key in ['04', '06']:
                    self.assertNotIn(20 if key == '04' else 10, missing['answer']['enum'])
                else:
                    self.assertEqual(missing['answer'], {'const': False})

    def test_kz_invalid_shape_and_missed_contradictions_are_rejected(self):
        full_span = {'start_message_id': 'model-check-ru:0', 'end_message_id': 'model-check-ru:1'}
        self.answers['05'].update(answer=True, evidence_message_ids=[], missed=False, checked_span=full_span)
        with self.assertRaisesRegex(ValueError, 'requires evidence'):
            self.score_answers()
        self.answers['05']['evidence_message_ids'] = ['model-check-ru:1']
        for key, value in [('02', None), ('01', True), ('04', 20), ('06', 10)]:
            old = dict(self.answers[key])
            self.answers[key].update(answer=value, missed=True, checked_span=full_span, na_reason='Synthetic N/A')
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'missed action'):
                self.score_answers()
            self.answers[key] = old
        self.answers['04'].update(answer=15, missed=True, evidence_message_ids=[], checked_span=full_span)
        self.assertEqual(self.score_answers()['total'], 95)

    def test_continuous_custom_scale_missing_branch_excludes_maximum(self):
        checklist = official_checklist()
        criterion = checklist['criteria'][3]
        del criterion['allowed_values']
        schema = _response_schema(validate_checklist(checklist), self.conversation)
        missing = schema['properties']['answers']['properties']['04']['anyOf'][1]['properties']['answer']
        self.assertEqual(missing, {'type': 'number', 'minimum': 0, 'exclusiveMaximum': 20})


    def test_truncated_generation_is_rejected_even_with_complete_json(self):
        for envelope in ({'done': False}, {'done': True, 'done_reason': 'length'}):
            with self.subTest(envelope=envelope), self.assertRaisesRegex(ValueError, 'invalid structured evaluation'):
                self.call_provider({'answers': self.answers}, **envelope)


class SplitEvidenceTests(unittest.TestCase):
    """Model output uses operator/context evidence fields; storage keeps one list."""

    def setUp(self):
        self.checklist = official_checklist()
        self.conversation = synthetic_conversation('RU')

    def provider_output(self, **evidence):
        answers = {}
        for criterion in self.checklist['criteria']:
            value = criterion['max'] if criterion['type'] == 'numeric' else criterion['id'] != '08'
            answers[criterion['id']] = dict(answer=value, explanation='Synthetic split evidence', missed=False,
                                            gross_error=False, checked_span=None, na_reason='', **evidence)
        return answers

    def run_provider(self, answers):
        return ModelProviderTests.call_provider(self, {'answers': answers}, done=True)

    def test_split_fields_merge_operator_first_without_duplicates(self):
        result, _ = self.run_provider(self.provider_output(operator_evidence_ids=['model-check-ru:1'],
                                                           context_evidence_ids=['model-check-ru:0']))
        for answer in result:
            self.assertEqual(answer['evidence_message_ids'], ['model-check-ru:1', 'model-check-ru:0'])
            self.assertNotIn('operator_evidence_ids', answer)
            self.assertNotIn('context_evidence_ids', answer)

    def test_mixing_split_and_legacy_fields_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'invalid structured evaluation'):
            self.run_provider(self.provider_output(operator_evidence_ids=['model-check-ru:1'],
                                                   context_evidence_ids=[],
                                                   evidence_message_ids=['model-check-ru:0']))

    def test_split_evidence_roles_are_checked_against_source(self):
        for operator, context in [(['model-check-ru:0'], []),
                                  (['model-check-ru:1'], ['model-check-ru:1']),
                                  (['invented-id'], []), ([], ['model-check-ru:0'])]:
            with self.subTest(operator=operator, context=context), \
                    self.assertRaisesRegex(ValueError, 'invalid structured evaluation'):
                self.run_provider(self.provider_output(operator_evidence_ids=operator,
                                                       context_evidence_ids=context))

    def test_conversation_without_operator_keeps_single_evidence_field(self):
        conversation = dict(self.conversation, transcript=[
            dict(m, speaker='bot') for m in self.conversation['transcript']])
        fields = _response_schema(validate_checklist(self.checklist), conversation)[
            'properties']['answers']['properties']['01']['anyOf'][0]['properties']
        self.assertIn('evidence_message_ids', fields)
        self.assertNotIn('operator_evidence_ids', fields)
        self.assertEqual(fields['evidence_message_ids']['minItems'], 1)


if __name__ == '__main__':
    unittest.main()
