"""Release regression cases using synthetic records and isolated SQLite storage."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from server.app import APIError, Application
from server.storage import Store


class ReleaseAcceptanceTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / '.local' / 'tests'
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'release.sqlite3'
        self.app = Application(Store(self.path), provider=self.provider)
        self.record = {
            'conversation_id': 'release-synthetic', 'language': 'RU',
            'channel': 'voice call', 'audio_url': None,
            'transcript': [
                {'timestamp': '2026-11-07T12:00:00.123456+05:00', 'speaker': 'system', 'text': 'Synthetic start.'},
                {'timestamp': '2026-11-07T12:00:04.123456+05:00', 'speaker': 'bot', 'text': 'Synthetic routing.'},
                {'timestamp': '2026-11-07T12:00:09.123456+05:00', 'speaker': 'client', 'text': 'Synthetic request.'},
                {'timestamp': '2026-11-07T12:00:12.123456+05:00', 'speaker': 'operator', 'text': 'Synthetic answer.'},
            ],
        }

    @staticmethod
    def provider(checklist, conversation):
        return [{'criterion_id': c['id'], 'answer': True,
                 'explanation': 'Synthetic structural provider, not model output.',
                 'evidence_message_ids': [conversation['transcript'][-1]['id']]}
                for c in checklist['criteria']]

    def import_record(self, record=None):
        return self.app.dispatch('POST', '/api/imports', {
            'source': 'json', 'group_id': 'SYNTHETIC_RELEASE',
            'payload': [record or self.record]})

    def test_invalid_last_record_leaves_entire_import_unwritten(self):
        invalid = deepcopy(self.record)
        invalid['conversation_id'] = 'invalid-last'
        invalid['transcript'][-1]['speaker'] = 'unknown-role'
        with self.assertRaises(ValueError):
            self.app.dispatch('POST', '/api/imports', {'payload': [self.record, invalid]})
        self.assertEqual(self.app.dispatch('GET', '/api/conversations')['items'], [])

    def test_conflicting_last_record_does_not_partially_import_first(self):
        self.import_record()
        fresh = deepcopy(self.record)
        fresh['conversation_id'] = 'fresh-first'
        conflict = deepcopy(self.record)
        conflict['transcript'][-1]['text'] = 'Changed synthetic source.'
        with self.assertRaises(APIError):
            self.app.dispatch('POST', '/api/imports', {'payload': [fresh, conflict]})
        self.assertEqual([c['conversation_id'] for c in
                          self.app.dispatch('GET', '/api/conversations')['items']],
                         ['release-synthetic'])

    def test_reimport_preserves_roles_precise_time_and_is_idempotent_after_restart(self):
        self.import_record()
        self.import_record()
        reopened = Application(Store(self.path))
        loaded = reopened.required('conversations', self.record['conversation_id'])
        self.assertEqual(len(reopened.dispatch('GET', '/api/conversations')['items']), 1)
        self.assertEqual([(m['speaker'], m['timestamp'], m['text']) for m in loaded['transcript']],
                         [(m['speaker'], m['timestamp'], m['text']) for m in self.record['transcript']])
        self.assertEqual(loaded['transcript'][-1]['audio_offset_seconds'], 12)

    def test_independent_appeals_compose_and_preserve_original_after_restart(self):
        self.import_record()
        checklist = self.app.dispatch('POST', '/api/checklists', {
            'name': 'Synthetic release rubric', 'provenance': 'custom',
            'criteria': [{'id': str(i), 'title': 'Synthetic criterion',
                          'type': 'boolean', 'weight': weight}
                         for i, weight in enumerate((1, 3))]})
        evaluation = self.app.dispatch('POST', '/api/evaluations', {
            'conversation_id': self.record['conversation_id'], 'checklist_id': checklist['id']})
        appeals = [self.app.dispatch('POST', '/api/appeals', {
            'evaluation_id': evaluation['id'], 'criterion_id': str(i),
            'comment': 'Synthetic independent review.'}) for i in (0, 1)]
        totals = []
        for i, appeal in enumerate(appeals):
            replacement = deepcopy(evaluation['final']['answers'][i])
            replacement['answer'] = False
            reviewed = self.app.dispatch('POST', '/api/appeals/' + appeal['id'] + '/review', {
                'decision': 'change', 'reason': 'Synthetic reviewer rationale.', 'answer': replacement})
            totals.append(reviewed['evaluation']['final']['result']['total'])
        self.assertEqual(totals, [75, 0])
        reopened = Application(Store(self.path))
        saved = reopened.required('evaluations', evaluation['id'])
        self.assertEqual(saved['original'], evaluation['original'])
        self.assertEqual(saved['final']['result']['total'], 0)
        self.assertEqual(len(reopened.dispatch('GET', '/api/appeals')['items']), 2)

    def test_failed_evaluation_creates_no_partial_result(self):
        self.import_record()
        def failing(*_):
            raise ValueError('Synthetic provider failure.')
        self.app.provider = failing
        with self.assertRaises(ValueError):
            self.app.dispatch('POST', '/api/evaluations', {
                'conversation_id': self.record['conversation_id'], 'checklist_id': 'official-cqa-v1'})
        self.assertEqual(self.app.dispatch('GET', '/api/evaluations')['items'], [])


if __name__ == '__main__':
    unittest.main()
