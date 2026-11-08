"""Cloud CLI wrapper tests; fake subprocess only, never real model inference."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch

from server.app import Application
from server.storage import Store
from tools.evaluate_external import ROOT, run_worker, source_view


class ExternalCLIWrapperTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / '.local/tests'
        parent.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.store = Store(self.directory / 'external.sqlite3')
        app = Application(self.store)
        app.dispatch('POST', '/api/imports', {'group_id': 'DEV', 'payload': [{
            'conversation_id': 'synthetic-cloud', 'channel': 'agent chat', 'language': 'RU',
            'audio_url': None, 'private_metadata': 'must not leave source store',
            'transcript': [{'timestamp': '00:00', 'speaker': 'client', 'text': 'Synthetic request.'},
                           {'timestamp': '00:01', 'speaker': 'operator', 'text': 'Synthetic answer.'}],
        }]})
        self.source = self.store.get('conversations', 'synthetic-cloud')
        self.args = SimpleNamespace(codex='synthetic-cli-only', model='synthetic-cloud-model', group_id='DEV')

    def test_model_input_preserves_only_required_source_fields(self):
        before = deepcopy(self.source)
        view = source_view(self.source)
        self.assertEqual(set(view), {'conversation_id', 'channel', 'language', 'transcript'})
        self.assertNotIn('audio_url', view)
        self.assertNotIn('private_metadata', view)
        self.assertEqual(set(view['transcript'][0]), {'id', 'timestamp', 'speaker', 'text'})
        self.assertEqual(view['transcript'][1]['text'], self.source['transcript'][1]['text'])
        self.assertEqual(self.source, before)

    def test_failed_cli_creates_no_fabricated_evaluation(self):
        with patch('tools.evaluate_external.subprocess.run', return_value=SimpleNamespace(returncode=1)):
            result = run_worker(self.store, [self.source], self.args, self.directory, 1)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(self.store.list('evaluations'), [])
        self.assertEqual(self.store.get('conversations', 'synthetic-cloud'), self.source)

    def test_wrong_output_ids_are_rejected_before_any_storage(self):
        def fake(command, **kwargs):
            target = Path(command[command.index('-o') + 1])
            target.write_text(json.dumps({'evaluations': [{'conversation_id': 'not-the-source', 'answers': []}]}), encoding='utf-8')
            return SimpleNamespace(returncode=0)
        with patch('tools.evaluate_external.subprocess.run', side_effect=fake):
            result = run_worker(self.store, [self.source], self.args, self.directory, 2)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(self.store.list('evaluations'), [])
