"""Synthetic appeal regressions; no real provider or private data is used."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from server.app import Application
from server.storage import Store


class AppealEvidenceRoleTests(unittest.TestCase):
    def setUp(self):
        runtime = Path(__file__).resolve().parents[1] / '.local' / 'tests'
        runtime.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=runtime)
        self.app = Application(Store(Path(self.tmp.name) / 'appeals.sqlite3'), provider=self.provider)
        self.app.dispatch('POST', '/api/imports', {'source': 'json', 'payload': [{
            'conversation_id': 'role-review', 'language': 'EN', 'channel': 'agent chat',
            'audio_url': None, 'transcript': [
                {'timestamp': '00:00', 'speaker': 'client', 'text': 'Synthetic question.'},
                {'timestamp': '00:01', 'speaker': 'bot', 'text': 'Hello, synthetic bot.'},
                {'timestamp': '00:02', 'speaker': 'operator', 'text': 'Synthetic operator reply.'},
                {'timestamp': '00:03', 'speaker': 'system', 'text': 'Synthetic conversation ended.'},
            ]}]})
        self.evaluation = self.app.dispatch('POST', '/api/evaluations', {
            'conversation_id': 'role-review', 'checklist_id': 'official-cqa-v1'})

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def provider(checklist, _conversation):
        return [{'criterion_id': c['id'],
                 'answer': c['max'] if c['type'] == 'numeric' else c['id'] not in ('01', '08'),
                 'explanation': 'Synthetic role review finding.',
                 'evidence_message_ids': ['role-review:2']}
                for c in checklist['criteria']]

    def appeal(self, criterion='01'):
        return self.app.dispatch('POST', '/api/appeals', {
            'evaluation_id': self.evaluation['id'], 'criterion_id': criterion,
            'comment': 'Synthetic evidence review.'})

    def answer(self, criterion='01', **updates):
        answer = next(deepcopy(a) for a in self.evaluation['final']['answers']
                      if a['criterion_id'] == criterion)
        answer.update(updates)
        return answer

    def review(self, appeal, answer=None, decision='change'):
        return self.app.dispatch('POST', f"/api/appeals/{appeal['id']}/review", {
            'decision': decision, 'reason': 'Synthetic reviewer decision.', 'answer': answer})

    def stored(self):
        return self.app.required('evaluations', self.evaluation['id'])

    def test_context_only_affirmative_replacement_is_rejected_atomically(self):
        appeal = self.appeal()
        for role, index in [('client', 0), ('bot', 1), ('system', 3)]:
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, 'operator evidence'):
                self.review(appeal, self.answer(answer=True, evidence_message_ids=[f'role-review:{index}']))
            self.assertEqual(self.app.required('appeals', appeal['id']), appeal)
            self.assertEqual(self.stored(), self.evaluation)

    def test_operator_with_optional_context_is_accepted_and_original_preserved(self):
        result = self.review(self.appeal(), self.answer(
            answer=True, evidence_message_ids=['role-review:2', 'role-review:0', 'role-review:3']))
        self.assertEqual(result['evaluation']['final']['result']['total'], 100)
        self.assertEqual(result['evaluation']['original'], self.evaluation['original'])
        self.assertEqual(result['appeal']['status'], 'reviewed')
        self.assertEqual(self.stored(), result['evaluation'])

    def test_positive_gross_error_requires_operator_evidence(self):
        appeal = self.appeal('08')
        with self.assertRaisesRegex(ValueError, 'operator evidence'):
            self.review(appeal, self.answer('08', answer=True, evidence_message_ids=['role-review:3']))
        self.assertEqual(self.app.required('appeals', appeal['id']), appeal)
        self.assertEqual(self.stored(), self.evaluation)

    def test_na_can_use_system_context(self):
        result = self.review(self.appeal('07'), self.answer(
            '07', answer=None, na_reason='Synthetic interruption before closing opportunity.',
            evidence_message_ids=['role-review:3']))
        self.assertIsNone(next(a for a in result['evaluation']['final']['answers']
                               if a['criterion_id'] == '07')['answer'])
        self.assertEqual(result['evaluation']['original'], self.evaluation['original'])

    def test_missed_action_accepts_checked_range_without_operator_quote(self):
        result = self.review(self.appeal(), self.answer(
            answer=False, missed=True, evidence_message_ids=[],
            checked_span={'start_message_id': 'role-review:0', 'end_message_id': 'role-review:3'}))
        self.assertEqual(result['evaluation']['final']['result']['total'], 90)
        self.assertEqual(result['evaluation']['original'], self.evaluation['original'])

    def test_negative_gross_accepts_full_checked_range(self):
        result = self.review(self.appeal('08'), self.answer(
            '08', answer=False, evidence_message_ids=[],
            checked_span={'start_message_id': 'role-review:0', 'end_message_id': 'role-review:3'}))
        self.assertFalse(result['evaluation']['final']['result']['gross_error'])
        self.assertEqual(result['evaluation']['original'], self.evaluation['original'])

    def legacy_evidence(self):
        # Historical answers remain inspectable/confirmable. Only a new
        # replacement is subject to the new source-role validation.
        e = deepcopy(self.evaluation)
        e['original']['answers'][0]['evidence_message_ids'] = ['role-review:1']
        e['final']['answers'][0]['evidence_message_ids'] = ['role-review:1']
        self.app.store.put('evaluations', e['id'], e)
        return e

    def test_confirm_keeps_historical_evidence_unchanged(self):
        historical = self.legacy_evidence()
        result = self.review(self.appeal(), decision='confirm')
        self.assertEqual(result['evaluation']['original'], historical['original'])
        self.assertEqual(result['evaluation']['final']['answers'], historical['final']['answers'])
        self.assertEqual(result['appeal']['review']['decision'], 'confirm')

    def test_unrelated_historical_row_does_not_block_valid_correction(self):
        historical = self.legacy_evidence()
        result = self.review(self.appeal('06'), self.answer('06', answer=7))
        self.assertEqual(result['evaluation']['original'], historical['original'])
        self.assertEqual(result['evaluation']['final']['answers'][0], historical['final']['answers'][0])
        self.assertEqual(result['evaluation']['final']['result']['total'], 85.5)


if __name__ == '__main__':
    unittest.main()
