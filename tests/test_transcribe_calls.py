"""Local ASR CLI regressions using synthetic bytes and a fake model, no inference."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import sys
import unittest
from unittest.mock import patch, Mock

from server.storage import Store
from tools.transcribe_calls import (
    ROOT, download_audio, main, normalize_segments, project_path, transcribe_one,
    refine_segment_languages,
)


class TranscribeCallsTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / '.local/tests'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_project_paths_cannot_escape_workspace(self):
        self.assertTrue(project_path('.local/audio-cache').is_relative_to(ROOT))
        with self.assertRaises(ValueError):
            project_path(ROOT.parent / 'outside-audio')

    def test_short_confident_kazakh_switch_is_redecoded_in_its_audio_span(self):
        calls = []
        class Model:
            def detect_language(self, *, audio):
                return 'kk', 0.97, [('kk', 0.97)]
            def transcribe(self, audio, **kwargs):
                calls.append((len(audio), kwargs))
                return iter([SimpleNamespace(start=0.1, end=1.8, text=' Сәлеметсіз бе! ')]), None
        segments = [{'id': 'asr-9', 'start': 2.0, 'end': 4.0, 'text': 'Original noisy recognition'}]
        result = refine_segment_languages(Model(), segments, [0] * 96000, 6, 'ru')
        self.assertEqual(calls[0][0], 32000)
        self.assertEqual(calls[0][1]['language'], 'kk')
        self.assertFalse(calls[0][1]['multilingual'])
        self.assertEqual(result, [{'id': 'asr-0', 'start': 2.1, 'end': 3.8, 'text': 'Сәлеметсіз бе!', 'language': 'kk'}])
        self.assertEqual(segments[0]['text'], 'Original noisy recognition')

    def test_uncertain_or_unsupported_language_does_not_rewrite_segment(self):
        for lang, probability in [('kk', 0.6), ('fr', 0.99), ('ru', 1.0)]:
            model = SimpleNamespace(detect_language=lambda **kw: (lang, probability, []),
                                    transcribe=Mock(side_effect=AssertionError('No rewrite expected')))
            row = {'id': 'asr-8', 'start': 0.0, 'end': 2.0, 'text': 'Keep speech text'}
            result = refine_segment_languages(model, [row], [0] * 32000, 2, 'ru')
            self.assertEqual(result, [dict(row, id='asr-0', language='ru')])
            model.transcribe.assert_not_called()

    def test_segment_validation_rejects_invalid_time_and_never_assigns_roles(self):
        segment = SimpleNamespace(start=0.0, end=1.0, text=' Synthetic recognised speech. ')
        self.assertEqual(normalize_segments([segment], 2), [{
            'id': 'asr-0', 'start': 0.0, 'end': 1.0, 'text': 'Synthetic recognised speech.',
        }])
        for start, end in [(-1, 1), (1, 0), (0, float('inf')), (1, 4), (True, 1)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                normalize_segments([SimpleNamespace(start=start, end=end, text='Synthetic')], 2)

    def test_transcribe_options_and_recording_provenance(self):
        audio = self.root / 'synthetic.mp3'
        audio.write_bytes(b'fake-model-input-not-a-real-recording')
        calls = []

        class Model:
            def transcribe(self, path, **options):
                calls.append((path, options))
                return iter([SimpleNamespace(start=0.2, end=1.1, text='Synthetic recognised speech.')]), SimpleNamespace(duration=2.5, language='ru')

        result = transcribe_one(Model(), {'conversation_id': 'synthetic-voice', 'language': 'RU'},
                                audio, 'synthetic-model', 'cpu', 'int8')
        self.assertEqual(calls[0][1], {
            'language': 'ru', 'beam_size': 5, 'temperature': 0.0, 'vad_filter': True,
            'word_timestamps': False, 'condition_on_previous_text': False, 'multilingual': True,
        })
        self.assertEqual(result['source'], 'recording')
        self.assertFalse(result['reviewed'])
        self.assertEqual(result['language'], 'ru')
        self.assertEqual(result['original_language'], 'RU')
        self.assertEqual(result['speaker_attribution'], 'unassigned')
        self.assertNotIn('speaker', result['segments'][0])
        self.assertNotIn('transcript', result)

    def test_ready_batch_skips_without_loading_model_or_changing_original(self):
        database = self.root / 'synthetic.sqlite3'
        store = Store(database)
        source = {'conversation_id': 'synthetic-voice', 'channel': 'voice call',
                  'language': 'RU', 'transcript': [{'text': 'Original ASR stays unchanged.'}]}
        store.put('conversations', source['conversation_id'], source)
        saved = {'conversation_id': source['conversation_id'], 'status': 'ready',
                 'source': 'recording', 'model': 'synthetic', 'language': 'ru',
                 'created_at': '2026-11-08T05:00:00+00:00', 'reviewed': False,
                 'audio_duration_seconds': 2,
                 'segments': [{'id': 'asr-0', 'start': 0, 'end': 1, 'text': 'Synthetic recognised text.'}]}
        store.put('transcriptions', source['conversation_id'], saved)
        with redirect_stdout(io.StringIO()) as output:
            result = main(['--database', str(database)])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())['skipped'], 1)
        self.assertEqual(store.get('conversations', source['conversation_id']), source)
        self.assertEqual(store.get('transcriptions', source['conversation_id']), saved)

    def test_invalid_ready_record_is_reprocessed_without_erasing_it_on_failure(self):
        database = self.root / 'retry.sqlite3'
        store = Store(database)
        cid = 'synthetic-retry'
        source = {'conversation_id': cid, 'channel': 'voice call', 'language': 'RU'}
        store.put('conversations', cid, source)
        invalid = {'conversation_id': cid, 'status': 'ready', 'segments': []}
        store.put('transcriptions', cid, invalid)
        factory = Mock()
        with redirect_stdout(io.StringIO()) as output, \
                patch.dict(sys.modules, {'faster_whisper': SimpleNamespace(WhisperModel=factory)}), \
                patch('tools.transcribe_calls.configure_local_caches'), \
                patch('tools.transcribe_calls.configure_windows_dlls'), \
                patch('tools.transcribe_calls.download_audio', side_effect=ValueError('Synthetic offline failure')):
            code = main(['--database', str(database), '--model-dir', str(self.root / 'model')])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue().splitlines()[0])['pending'], 1)
        factory.assert_called_once()
        self.assertEqual(store.get('transcriptions', cid), invalid)
        self.assertEqual(store.get('conversations', cid), source)

    def test_failed_download_does_not_leave_partial_cache_or_log_signed_url(self):
        class Response:
            headers = {'Content-Length': '0'}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def read(self, _size):
                return b''

        class Opener:
            def open(self, _request, timeout):
                return Response()

        cache = self.root / 'cache'
        with patch('tools.transcribe_calls.resolve_audio', return_value={
            'status': 'playable', 'media_url': 'https://synthetic.invalid/audio?sig=PRIVATE_TEST_SENTINEL',
        }):
            with self.assertRaisesRegex(ValueError, 'empty'):
                download_audio({'conversation_id': '../synthetic', 'audio_url': 'synthetic'}, cache, Opener())
        self.assertEqual(list(cache.iterdir()), [])

    def test_existing_cache_reuses_hashed_name_without_network(self):
        import hashlib
        conversation = {'conversation_id': '../synthetic-with-path'}
        destination = self.root / (hashlib.sha256(conversation['conversation_id'].encode()).hexdigest() + '.mp3')
        destination.write_bytes(b'synthetic-cache-only')
        with patch('tools.transcribe_calls.resolve_audio', side_effect=AssertionError('No network expected')):
            self.assertEqual(download_audio(conversation, self.root), destination)


if __name__ == '__main__':
    unittest.main()
