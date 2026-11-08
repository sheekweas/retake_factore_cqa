"""Bounded departure acceptance regressions; transport is explicitly mocked."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from server.semantic_check import main, synthetic_fixture


class DepartureAcceptanceTests(unittest.TestCase):
    @staticmethod
    def departure_answers(checklist, conversation):
        _, expected, _ = synthetic_fixture(conversation['language'], 'departure')
        turns = conversation['transcript']
        return [{
            'criterion_id': criterion['id'],
            'answer': expected.get(criterion['id'], criterion.get('max', True)),
            'explanation': 'TEST ONLY: labelled departure response',
            'evidence_message_ids': [turns[1]['id']],
            'checked_span': {'start_message_id': turns[0]['id'],
                             'end_message_id': turns[-1]['id']},
            'na_reason': 'TEST ONLY: interrupted before request' if expected.get(criterion['id'], True) is None else '',
        } for criterion in checklist['criteria']]

    def run_departure(self, provider):
        with patch('server.semantic_check.infer', side_effect=provider), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(['--language', 'RU', '--language', 'KZ', '--scenario', 'departure'])
        return code, json.loads(output.getvalue())

    def test_two_language_departure_selection_never_repeats_completed_or_en_cases(self):
        calls = []

        def provider(checklist, conversation):
            calls.append((conversation['language'], conversation['conversation_id']))
            return self.departure_answers(checklist, conversation)

        code, report = self.run_departure(provider)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [('RU', 'semantic-ru-departure'), ('KZ', 'semantic-kz-departure')])
        self.assertEqual([(row['language'], row['scenario']) for row in report['checks']],
                         [('RU', 'departure'), ('KZ', 'departure')])
        self.assertTrue(all(row['semantic_status'] == 'passed' for row in report['checks']))

    def test_semantic_failure_in_first_language_does_not_hide_second_language_result(self):
        def provider(checklist, conversation):
            answers = self.departure_answers(checklist, conversation)
            if conversation['language'] == 'RU':
                # Valid JSON/scoring structure, but greeting denied despite explicit greeting/name.
                answers[0]['answer'] = False
            return answers

        code, report = self.run_departure(provider)
        self.assertEqual(code, 1)
        self.assertEqual(report['structural_status'], 'passed')
        self.assertEqual(report['semantic_status'], 'failed')
        self.assertEqual([(row['language'], row['semantic_status']) for row in report['checks']],
                         [('RU', 'failed'), ('KZ', 'passed')])


if __name__ == '__main__':
    unittest.main()
