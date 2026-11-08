"""Recording text stays separate from immutable role-tagged scoring evidence."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from server.app import Application
from server.storage import Store
from server.transcriptions import validate_transcription


class RecordingTranscriptionTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / '.local' / 'tests'
        root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'recordings.sqlite3')
        self.app = Application(self.store)
        self.app.dispatch('POST', '/api/imports', {'payload': [{
            'conversation_id': 'recording-fixture', 'language': 'RU', 'channel': 'voice call',
            'audio_url': None, 'transcript': [
                {'timestamp': '2026-11-03T15:49:13.2744711+00:00', 'speaker': 'client', 'text': 'Исходный вопрос.'},
                {'timestamp': '2026-11-03T15:49:22.4171535+00:00', 'speaker': 'operator', 'text': 'Исходный ответ.'},
            ],
        }]})
        self.source = self.store.get('conversations', 'recording-fixture')
        self.record = {
            'conversation_id': 'recording-fixture', 'status': 'ready', 'source': 'recording',
            'model': 'local-speech-model', 'language': 'ru', 'created_at': '2026-11-08T05:00:00+00:00',
            'reviewed': False, 'audio_duration_seconds': 12,
            'segments': [{'id': 'asr-0', 'start': 0, 'end': 4.2, 'text': 'Текст из записи.'}],
        }

    def test_read_enrichment_preserves_source_and_precise_timestamps(self):
        self.store.put('transcriptions', 'recording-fixture', self.record)
        response = self.app.dispatch('GET', '/api/conversations/recording-fixture')
        self.assertEqual(response['transcript'], self.source['transcript'])
        self.assertEqual(response['recording_transcription'], self.record)
        self.assertEqual(self.store.get('conversations', 'recording-fixture'), self.source)
        response['recording_transcription']['segments'][0]['text'] = 'Mutated read view'
        self.assertEqual(self.store.get('transcriptions', 'recording-fixture'), self.record)

    def test_bad_sidecar_does_not_hide_original(self):
        bad = deepcopy(self.record)
        bad['segments'][0]['end'] = 500
        self.store.put('transcriptions', 'recording-fixture', bad)
        response = self.app.dispatch('GET', '/api/conversations/recording-fixture')
        self.assertEqual(response['recording_transcription']['status'], 'invalid')
        self.assertEqual(response['transcript'], self.source['transcript'])

    def test_no_sidecar_is_backward_compatible(self):
        self.assertEqual(self.app.dispatch('GET', '/api/conversations/recording-fixture'), self.source)

    def test_rejects_wrong_conversation_and_unverified_speaker_roles(self):
        with self.assertRaises(ValueError):
            validate_transcription(self.record, 'another-conversation')
        for field, value in [('speaker', 'operator')]:
            bad = deepcopy(self.record)
            bad['segments'][0][field] = value
            with self.assertRaises(ValueError):
                validate_transcription(bad, 'recording-fixture')

    def test_rejects_invalid_offsets_duplicate_ids_and_review_claim(self):
        invalid = []
        for field, value in [('start', float('nan')), ('end', float('inf')), ('start', True), ('start', -1), ('end', 99)]:
            bad = deepcopy(self.record)
            bad['segments'][0][field] = value
            invalid.append(bad)
        duplicate = deepcopy(self.record)
        duplicate['segments'].append(deepcopy(duplicate['segments'][0]))
        invalid.append(duplicate)
        reviewed = deepcopy(self.record)
        reviewed['reviewed'] = True
        invalid.append(reviewed)
        for bad in invalid:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_transcription(bad, 'recording-fixture')

    def test_evaluation_inputs_remain_role_tagged_source(self):
        self.store.put('transcriptions', 'recording-fixture', self.record)
        seen = []
        def provider(checklist, conversation):
            seen.append(deepcopy(conversation))
            return [{'criterion_id': 'greeting', 'answer': True, 'explanation': 'Fixture operator evidence.',
                     'evidence_message_ids': [conversation['transcript'][1]['id']]}]
        self.app.provider = provider
        checklist = self.app.dispatch('POST', '/api/checklists', {
            'name': 'Fixture', 'provenance': 'custom',
            'criteria': [{'id': 'greeting', 'title': 'Fixture', 'type': 'boolean', 'weight': 1}],
        })
        evaluated = self.app.dispatch('POST', '/api/evaluations', {
            'conversation_id': 'recording-fixture', 'checklist_id': checklist['id'],
        })
        self.assertEqual(seen, [self.source])
        self.assertEqual(evaluated['final']['answers'][0]['evidence_message_ids'], ['recording-fixture:1'])
        self.assertEqual(evaluated['original'], evaluated['final'])


if __name__ == '__main__':
    unittest.main()
