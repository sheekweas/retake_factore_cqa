"""Real HTTP checks for the local app's Host/Origin boundary."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from server.app import Application, make_server
from server.presets import official_checklist
from server.storage import Store


class LocalHostBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runtime = Path(__file__).resolve().parents[1] / '.local' / 'tests'
        runtime.mkdir(parents=True, exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(dir=runtime)
        cls.app = Application(Store(Path(cls.tmp.name) / 'host.sqlite3'))
        cls.server = make_server(cls.app, port=0)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def request(self, host=None, path='/api/health', method='GET', body=None, origin=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            payload = json.dumps(body).encode('utf-8') if body is not None else None
            connection.putrequest(method, path, skip_host=True)
            if host is not None:
                for value in host if isinstance(host, list) else [host]:
                    connection.putheader('Host', value)
            if origin is not None:
                connection.putheader('Origin', origin)
            if payload is not None:
                connection.putheader('Content-Type', 'application/json')
                connection.putheader('Content-Length', str(len(payload)))
            connection.endheaders(payload)
            response = connection.getresponse()
            raw = response.read()
            return response.status, raw
        finally:
            connection.close()

    def test_actual_loopback_hosts_accept_health_and_static(self):
        for host in (f'127.0.0.1:{self.port}', f'localhost:{self.port}', f'LOCALHOST:{self.port}'):
            with self.subTest(host=host):
                status, raw = self.request(host)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw)['status'], 'ok')
                self.assertEqual(self.request(host, path='/')[0], 200)

    def test_static_modules_are_not_stale_after_application_update(self):
        for path in ('/', '/app/dom.js', '/styles/app.css'):
            with self.subTest(path=path):
                connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
                try:
                    connection.request('GET', path)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader('Cache-Control'), 'no-store')
                    self.assertTrue(response.read())
                finally:
                    connection.close()

    def test_foreign_host_denied_for_read_write_static_and_unsupported_method(self):
        original = len(self.app.store.list('checklists'))
        hostile = f'attacker.example:{self.port}'
        for method, path, body in (
            ('GET', '/api/conversations', None),
            ('GET', '/', None),
            ('POST', '/api/checklists', official_checklist()),
            ('OPTIONS', '/', None),
            ('DELETE', '/api/checklists', None),
        ):
            with self.subTest(method=method, path=path):
                status, raw = self.request(hostile, path, method, body, 'http://' + hostile)
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(raw)['error']['code'], 'host')
        self.assertEqual(len(self.app.store.list('checklists')), original)

    def test_wrong_port_missing_ambiguous_or_noncanonical_hosts_are_denied(self):
        for host in (
            f'localhost:{self.port + 1}', f'127.0.0.1:{self.port + 1}',
            'localhost', '127.0.0.1', None,
            [f'localhost:{self.port}', f'127.0.0.1:{self.port}'],
            f'localhost:{self.port}@attacker.example', f'localhost:{self.port}/',
            f'127.1:{self.port}', f'[::1]:{self.port}',
        ):
            with self.subTest(host=host):
                self.assertEqual(self.request(host)[0], 403)

    def test_cli_post_without_origin_works_and_foreign_origin_stays_denied(self):
        host = f'127.0.0.1:{self.port}'
        self.assertEqual(self.request(host, '/api/checklists', 'POST', official_checklist())[0], 200)
        status, raw = self.request(host, '/api/checklists', 'POST', official_checklist(), 'https://attacker.example')
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw)['error']['code'], 'origin')
        self.assertEqual(self.request(host, '/api/checklists', 'POST', official_checklist(), 'http://' + host)[0], 200)


if __name__ == '__main__':
    unittest.main()
