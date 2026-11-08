"""Serial dataset runner tests; fake local API only, no model or network."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from server.presets import official_checklist
from tools.evaluate_dataset import BatchError, CHECKLIST_ID, ROOT, parse_args, provider_matches, run_batch, save_progress


class FakeAPI:
    def __init__(self, expected):
        self.expected = expected
        self.records = [{'conversation_id': 'short-chat', 'channel': 'agent chat', 'message_count': 2},
                        {'conversation_id': 'long-chat', 'channel': 'agent chat', 'message_count': 8},
                        {'conversation_id': 'voice', 'channel': 'voice call', 'message_count': 1}]
        self.history = []
        self.calls = []
        self.fail = set()
        self.complete_then_fail = set()
        self.stop_after = None

    def evaluation(self, cid):
        return {'id': 'synthetic-' + cid, 'conversation_id': cid,
                'checklist_id': CHECKLIST_ID, 'provider': deepcopy(self.expected),
                'created_at': '2026-11-08T00:00:00+00:00',
                'original': {'synthetic': True}, 'final': {'synthetic': True}}

    def request(self, route, body=None):
        if route == 'checklists/' + CHECKLIST_ID:
            return official_checklist()
        if route.startswith('conversations?'):
            return {'items': deepcopy(list(reversed(self.records)))}
        if route.startswith('evaluations?'):
            cid = parse_qs(urlsplit(route).query)['conversation_id'][0]
            return {'items': [deepcopy(e) for e in self.history if e['conversation_id'] == cid]}
        if route == 'evaluations' and body:
            cid = body['conversation_id']
            self.calls.append(cid)
            if cid in self.fail:
                raise BatchError('Synthetic model error; no score returned.')
            result = self.evaluation(cid)
            self.history.append(result)
            if self.stop_after:
                self.stop_after.touch()
            if cid in self.complete_then_fail:
                raise BatchError('Synthetic client timeout.')
            return deepcopy(result)
        raise AssertionError('Unexpected fake API request')


class EvaluateDatasetTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / '.local/tests'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        self.args = parse_args(['--progress-file', str(directory / 'progress.json'),
                                '--stop-file', str(directory / 'STOP'),
                                '--model', 'synthetic-model', '--think', '1'])
        self.expected = {'type': 'ollama', 'model': self.args.model,
                         'prompt_revision': self.args.prompt_revision, 'think': True}
        self.api = FakeAPI(self.expected)

    def run_silent(self):
        with redirect_stdout(io.StringIO()):
            return run_batch(self.args, self.api)

    def test_progress_survives_a_transient_windows_file_lock(self):
        path = Path(self.args.progress_file)
        progress = {'conversations': {'synthetic': {'status': 'ok'}}}
        original = Path.replace
        attempts = []
        def locked_once(source, destination):
            attempts.append(str(destination))
            if len(attempts) == 1:
                raise PermissionError('Synthetic Windows reader lock')
            return original(source, destination)
        with patch.object(Path, 'replace', autospec=True, side_effect=locked_once), \
                patch('tools.evaluate_dataset.time.sleep'):
            save_progress(path, progress)
        self.assertGreaterEqual(len(attempts), 2)
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['conversations'], progress['conversations'])
        self.assertEqual(saved['summary']['ok'], 1)

    def test_current_provider_matches_skip_and_calls_are_serial_short_chats_first(self):
        self.api.history.append(self.api.evaluation('long-chat'))
        result = self.run_silent()
        self.assertEqual(self.api.calls, ['short-chat', 'voice'])
        self.assertEqual(result['summary'], {'total': 3, 'ok': 3, 'failed': 0, 'pending': 0})
        self.assertEqual(result['conversations']['long-chat']['source'], 'existing')

    def test_failure_continues_and_records_error_without_fake_zero(self):
        self.api.fail.add('short-chat')
        result = self.run_silent()
        error = result['conversations']['short-chat']
        self.assertEqual(error['status'], 'error')
        self.assertNotIn('evaluation_id', error)
        self.assertNotIn('score', error)
        self.assertEqual(result['summary']['ok'], 2)
        self.assertEqual(result['summary']['failed'], 1)
        self.assertEqual(json.loads(Path(self.args.progress_file).read_text(encoding='utf-8')), result)

    def test_timeout_rechecks_history_before_any_second_model_call(self):
        self.api.complete_then_fail.add('short-chat')
        result = self.run_silent()
        self.assertEqual(self.api.calls.count('short-chat'), 1)
        self.assertEqual(result['summary']['ok'], 3)

    def test_stop_file_pauses_between_calls_preserving_completed_result(self):
        self.api.stop_after = Path(self.args.stop_file)
        result = self.run_silent()
        self.assertEqual(self.api.calls, ['short-chat'])
        self.assertEqual(result['state'], 'paused')
        self.assertEqual(result['summary'], {'total': 3, 'ok': 1, 'failed': 0, 'pending': 2})
        self.assertEqual(result['conversations']['short-chat']['evaluation_id'], 'synthetic-short-chat')

    def test_limit_keeps_all_real_ids_and_pending_entries(self):
        self.args.limit = 1
        result = self.run_silent()
        self.assertEqual(self.api.calls, ['short-chat'])
        self.assertEqual(result['state'], 'limited')
        self.assertEqual(set(result['conversations']), {'short-chat', 'long-chat', 'voice'})
        self.assertEqual(result['summary']['pending'], 2)

    def test_prior_errors_require_explicit_retry_and_history_is_reused(self):
        self.api.fail.add('short-chat')
        self.run_silent()
        self.api.fail.clear()
        self.api.calls.clear()
        second = self.run_silent()
        self.assertEqual(self.api.calls, [])
        self.assertEqual(second['summary']['failed'], 1)
        self.args.retry_errors = True
        third = self.run_silent()
        self.assertEqual(self.api.calls, ['short-chat'])
        self.assertEqual(third['summary']['ok'], 3)

    def test_test_provider_other_revision_or_untyped_think_never_count_as_current(self):
        original = self.api.evaluation('short-chat')
        self.assertTrue(provider_matches(original, self.expected))
        for field, value in [('type', 'injected'), ('model', 'other-model'),
                             ('prompt_revision', 'other-revision'), ('think', False), ('think', 1)]:
            changed = deepcopy(original)
            changed['provider'][field] = value
            with self.subTest(field=field, value=value):
                self.assertFalse(provider_matches(changed, self.expected))


if __name__ == '__main__':
    unittest.main()
