"""HTTP concurrency regression with an Event-blocked synthetic provider."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from server.app import Application, make_server
from server.storage import Store
from test_api import CONVERSATION, DEMO, test_answer


class InferenceConcurrencyTests(unittest.TestCase):
    def test_blocked_inference_keeps_navigation_and_review_available(self):
        runtime = Path(__file__).resolve().parents[1] / '.local' / 'tests'
        runtime.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime) as tmp:
            app = Application(Store(Path(tmp) / 'concurrency.sqlite3'), provider=lambda *_: [test_answer()])
            app.dispatch('POST', '/api/imports', {'payload': [CONVERSATION]})
            checklist = app.dispatch('POST', '/api/checklists', DEMO)
            payload = {'conversation_id': 'test-ru', 'checklist_id': checklist['id']}
            old = app.dispatch('POST', '/api/evaluations', payload)
            original = copy.deepcopy(old['original'])
            appeal = app.dispatch('POST', '/api/appeals', {
                'evaluation_id': old['id'], 'criterion_id': 'demo', 'comment': 'Synthetic concurrent review'})
            started, release = threading.Event(), threading.Event()
            outcome = {}

            def blocked_provider(*_):
                started.set()
                if not release.wait(8):
                    raise ValueError('Synthetic test provider release timeout')
                return [test_answer()]

            server = make_server(app, port=0)
            serving = threading.Thread(target=server.serve_forever, daemon=True)
            serving.start()
            base = 'http://127.0.0.1:' + str(server.server_port)

            def request(path, body=None, timeout=1.5):
                req = Request(base + path, data=None if body is None else json.dumps(body).encode(),
                              headers={'Content-Type': 'application/json'})
                with urlopen(req, timeout=timeout) as response:
                    return json.load(response)

            def evaluate():
                try:
                    outcome['result'] = request('/api/evaluations', payload, timeout=10)
                except Exception as exc:
                    outcome['error'] = exc

            worker = threading.Thread(target=evaluate, daemon=True)
            try:
                with patch('server.app.infer', side_effect=blocked_provider) as provider, \
                        patch.dict(os.environ, {'CQA_MODEL': 'synthetic-before', 'CQA_THINK': '1'}):
                    app.provider = provider
                    worker.start()
                    self.assertTrue(started.wait(2), 'Provider did not start')
                    os.environ['CQA_MODEL'] = 'synthetic-after'
                    self.assertEqual(request('/api/health')['status'], 'ok')
                    self.assertEqual(request('/api/conversations/test-ru')['conversation_id'], 'test-ru')
                    self.assertEqual(len(request('/api/checklists')['items']), 2)
                    self.assertEqual(len(request('/api/evaluations')['items']), 1)
                    self.assertEqual(request('/api/appeals/' + appeal['id'])['status'], 'pending')
                    changed = request('/api/appeals/' + appeal['id'] + '/review', {
                        'decision': 'change', 'reason': 'Synthetic review while provider blocked',
                        'answer': test_answer(False)})
                    self.assertEqual(changed['evaluation']['original'], original)
                    self.assertEqual(changed['evaluation']['final']['result']['total'], 0)
                    # A new checklist version cannot alter the in-flight snapshot.
                    newer = request('/api/checklists', DEMO | {'family_id': checklist['family_id'],
                                                             'name': 'Synthetic new version'})
                    self.assertEqual(newer['version'], 2)
                    release.set()
                    worker.join(3)
                    self.assertFalse(worker.is_alive())
                    self.assertNotIn('error', outcome)
                    result = outcome['result']
                    self.assertEqual(result['checklist_id'], checklist['id'])
                    self.assertEqual(result['provider']['model'], 'synthetic-before')
                    self.assertIs(result['provider']['think'], True)
                    self.assertEqual(result['original'], result['final'])
                    saved_old = request('/api/evaluations/' + old['id'])
                    self.assertEqual(saved_old['original'], original)
                    self.assertEqual(saved_old['final']['result']['total'], 0)
                    self.assertEqual(request('/api/evaluations/' + result['id']), result)
            finally:
                release.set()
                if worker.ident is not None:
                    worker.join(3)
                server.shutdown()
                server.server_close()
                serving.join(3)


if __name__ == '__main__':
    unittest.main()
