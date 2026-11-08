"""Audio link resolver tests. Synthetic URLs only; no external network."""
import functools
import socket
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import urlopen

from server import audio
from server.app import Application, make_server
from server.audio import resolve_audio
from server.storage import Store

PAGE = 'https://player.example.test/rec/abc'
MEDIA = 'https://media.example.test/files/abc.mp3?sig=TEST'
HTML = ('<html><body><h1>Запись</h1><audio id="myAudio" controls>'
        '<source src="%s" type="audio/mpeg"></audio></body></html>')


def public_dns(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', port))]


def fake(content_type, body=b'', final_url=None, calls=None):
    def fetch(url):
        if calls is not None:
            calls.append(url)
        return final_url or url, content_type, body
    return fetch


class ResolverTest(unittest.TestCase):
    def setUp(self):
        audio.clear_cache()
        patcher = mock.patch('server.audio.socket.getaddrinfo', side_effect=public_dns)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_html_page_with_source(self):
        r = resolve_audio(PAGE, fetch=fake('text/html', (HTML % MEDIA.replace('&', '&amp;')).encode()))
        self.assertEqual(r['status'], 'playable')
        self.assertEqual(r['media_url'], MEDIA)
        self.assertEqual(r['media_type'], 'audio/mpeg')
        self.assertEqual(r['page_url'], PAGE)

    def test_relative_source_resolved_against_page(self):
        r = resolve_audio(PAGE, fetch=fake('text/html; charset=utf-8', (HTML % '../media/x.mp3').encode()))
        self.assertEqual(r['media_url'], 'https://player.example.test/media/x.mp3')

    def test_audio_src_attribute(self):
        r = resolve_audio(PAGE, fetch=fake('text/html', b'<audio controls src="https://m.example.test/a.ogg"></audio>'))
        self.assertEqual((r['status'], r['media_url'], r['media_type']), ('playable', 'https://m.example.test/a.ogg', None))

    def test_source_outside_audio_ignored(self):
        r = resolve_audio(PAGE, fetch=fake('text/html', b'<video><source src="https://m.example.test/v.mp4"></video>'))
        self.assertEqual(r['status'], 'page_only')
        self.assertIsNone(r['media_url'])

    def test_direct_audio(self):
        r = resolve_audio(MEDIA, fetch=fake('audio/mpeg'))
        self.assertEqual((r['status'], r['media_url'], r['media_type']), ('playable', MEDIA, 'audio/mpeg'))

    def test_html_without_audio(self):
        r = resolve_audio(PAGE, fetch=fake('text/html', b'<html><p>no player</p></html>'))
        self.assertEqual((r['status'], r['media_url'], r['page_url']), ('page_only', None, PAGE))

    def test_other_content_type(self):
        self.assertEqual(resolve_audio(PAGE, fetch=fake('application/json', b'{}'))['status'], 'page_only')

    def test_none(self):
        for value in (None, ''):
            r = resolve_audio(value, fetch=fake('audio/mpeg'))
            self.assertEqual((r['status'], r['media_url'], r['page_url']), ('none', None, None))

    def test_bad_scheme_not_fetched(self):
        calls = []
        for url in ('http://example.test/a.mp3', 'ftp://example.test/a', 'javascript:alert(1)', 'file:///etc/passwd', 'http://127.0.0.1/a'):
            r = resolve_audio(url, fetch=fake('audio/mpeg', calls=calls))
            self.assertEqual(r['status'], 'error', url)
            self.assertIsNone(r['media_url'])
        self.assertEqual(calls, [])

    def test_javascript_source_rejected(self):
        for src in ('javascript:alert(1)', 'data:audio/mpeg;base64,AAAA', 'http://insecure.example.test/a.mp3'):
            r = resolve_audio(PAGE, fetch=fake('text/html', (HTML % src).encode()))
            self.assertEqual(r['status'], 'page_only', src)
            self.assertIsNone(r['media_url'])

    def test_network_error_never_raises_or_leaks(self):
        def broken(url):
            raise OSError('connection refused to ' + url)
        r = resolve_audio(PAGE + '?token=SECRET', fetch=broken)
        self.assertEqual(r['status'], 'error')
        self.assertNotIn('SECRET', r['message'])
        self.assertNotIn('example', r['message'])

    def test_cache_and_errors_not_cached(self):
        calls = []
        fetch = fake('text/html', (HTML % MEDIA).encode(), calls=calls)
        resolve_audio(PAGE, fetch=fetch)
        resolve_audio(PAGE, fetch=fetch)
        self.assertEqual(len(calls), 1)
        failures = []

        def broken(url):
            failures.append(url)
            raise TimeoutError()
        resolve_audio(PAGE + '2', fetch=broken)
        resolve_audio(PAGE + '2', fetch=broken)
        self.assertEqual(len(failures), 2)

    def test_body_capped(self):
        padding = b'<p>x</p>' * (audio.MAX_BYTES // 8 + 10)
        r = resolve_audio(PAGE, fetch=fake('text/html', padding + (HTML % MEDIA).encode()))
        self.assertEqual(r['status'], 'page_only')


class _Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == '/page':
            raw = (HTML % '/media.mp3').encode()
            kind = 'text/html; charset=utf-8'
        elif self.path == '/media.mp3':
            raw, kind = b'ID3', 'audio/mpeg'
        elif self.path == '/redirect-bad':
            self.send_response(302)
            self.send_header('Location', 'ftp://example.test/x')
            self.end_headers()
            return
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class DefaultFetchTest(unittest.TestCase):
    """Real urllib path against a loopback fixture (http allowed only for localhost here)."""

    def setUp(self):
        audio.clear_cache()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _Fixture)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:%d' % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_page_media_and_errors(self):
        r = resolve_audio(self.base + '/page', allow_http_localhost=True)
        self.assertEqual((r['status'], r['media_url'], r['media_type']), ('playable', self.base + '/media.mp3', 'audio/mpeg'))
        r = resolve_audio(self.base + '/media.mp3', allow_http_localhost=True)
        self.assertEqual((r['status'], r['media_type']), ('playable', 'audio/mpeg'))
        self.assertEqual(resolve_audio(self.base + '/missing', allow_http_localhost=True)['status'], 'error')
        self.assertEqual(resolve_audio(self.base + '/redirect-bad', allow_http_localhost=True)['status'], 'error')
        self.assertEqual(resolve_audio(self.base + '/page')['status'], 'error')  # http rejected by default


class AudioRouteTest(unittest.TestCase):
    def setUp(self):
        audio.clear_cache()
        runtime = Path(__file__).resolve().parents[1] / '.local' / 'tests'
        runtime.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=runtime)
        store = Store(Path(self.tmp.name) / 'test.sqlite3')
        self.app = Application(store, provider=lambda *_: [])
        transcript = [{'speaker': 'operator', 'timestamp': '00:01', 'text': 'Синтетическая реплика.'}]
        store.put_many([
            ('conversations', 'voice-1', {'conversation_id': 'voice-1', 'language': 'RU', 'channel': 'voice call', 'audio_url': PAGE, 'transcript': transcript}),
            ('conversations', 'chat-1', {'conversation_id': 'chat-1', 'language': 'RU', 'channel': 'agent chat', 'audio_url': None, 'transcript': transcript}),
        ])
        self.server = make_server(self.app, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:%d' % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def get(self, path):
        try:
            with urlopen(self.base + path, timeout=5) as r:
                return r.status, json.load(r)
        except HTTPError as e:
            with e:
                return e.code, json.load(e)

    def test_route(self):
        patched = functools.partial(resolve_audio, fetch=fake('text/html', (HTML % MEDIA).encode()))
        real_dns = socket.getaddrinfo
        def dns(host, port, *args, **kwargs):
            return public_dns(host, port, **kwargs) if host.endswith('.test') else real_dns(host, port, *args, **kwargs)
        with mock.patch('server.app.resolve_audio', patched), mock.patch('server.audio.socket.getaddrinfo', side_effect=dns):
            status, body = self.get('/api/conversations/voice-1/audio')
            self.assertEqual(status, 200, body)
            self.assertEqual((body['status'], body['media_url'], body['page_url']), ('playable', MEDIA, PAGE))
            self.assertEqual(self.get('/api/conversations/chat-1/audio')[1]['status'], 'none')
            status, body = self.get('/api/conversations/missing/audio')
            self.assertEqual(status, 404)
            self.assertEqual(body['error']['code'], 'not_found')
            self.assertEqual(self.app.dispatch('GET', '/api/conversations/voice-1/audio')['status'], 'playable')


class PublicAddressTest(unittest.TestCase):
    def setUp(self):
        audio.clear_cache()

    def test_nonpublic_literals_never_fetched(self):
        fetch = mock.Mock()
        for host in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '192.168.1.2',
                     '100.64.0.1', '0.0.0.0', '224.0.0.1', '[::1]', '[fc00::1]',
                     '[fe80::1]', '[::ffff:127.0.0.1]', '[ff02::1]'):
            with self.subTest(host=host):
                self.assertEqual(resolve_audio('https://' + host + '/recording', fetch=fetch)['status'], 'error')
        fetch.assert_not_called()

    def test_private_dns_mixed_answers_and_failures(self):
        public = public_dns('ignored', 443)
        private = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('10.0.0.7', 443))]
        for rows in (private, public + private, []):
            with mock.patch('server.audio.socket.getaddrinfo', return_value=rows):
                fetch = mock.Mock()
                self.assertEqual(resolve_audio(PAGE, fetch=fetch)['status'], 'error')
                fetch.assert_not_called()
        with mock.patch('server.audio.socket.getaddrinfo', side_effect=socket.gaierror()):
            self.assertEqual(resolve_audio(PAGE)['status'], 'error')

    def test_credentials_invalid_port_and_controls(self):
        for url in ('https://user:pass@example.test/a', 'https://example.test:bad/a',
                    'https://example.test\\@127.0.0.1/a', 'https://example.test/\nsecret'):
            self.assertEqual(resolve_audio(url, fetch=mock.Mock())['status'], 'error')

    def test_redirect_and_embedded_private_media_rejected(self):
        with mock.patch('server.audio.socket.getaddrinfo', side_effect=public_dns):
            handler = audio._SafeRedirect(False)
            for target in ('https://127.0.0.1/private', 'https://[::1]/private'):
                with self.assertRaises(audio.AudioFetchError):
                    handler.redirect_request(mock.Mock(), None, 302, '', {}, target)
                result = resolve_audio(PAGE, fetch=fake('text/html', (HTML % target).encode()), use_cache=False)
                self.assertEqual(result['status'], 'page_only')
            self.assertEqual(resolve_audio(PAGE, fetch=fake('audio/mpeg', final_url='https://127.0.0.1/a'))['status'], 'error')

    def test_public_hostname_pinned_without_second_dns_lookup(self):
        with mock.patch('server.audio.socket.getaddrinfo', side_effect=public_dns) as dns, mock.patch('server.audio.socket.socket') as factory:
            connection = audio._PublicHTTPSConnection('player.example.test')
            sock = connection._create_connection(('player.example.test', 443), 10)
            self.assertEqual(connection.host, 'player.example.test')  # retained for TLS/SNI
            dns.assert_called_once()
            factory.return_value.connect.assert_called_once_with(('93.184.216.34', 443))
            self.assertIs(sock, factory.return_value)

    def test_rebinding_on_connect_is_rejected(self):
        with mock.patch('server.audio.socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]), mock.patch('server.audio.socket.socket') as factory:
            with self.assertRaises(audio.AudioFetchError):
                audio._PublicHTTPSConnection('player.example.test')._create_connection(('player.example.test', 443), 10)
            factory.assert_not_called()

    def test_connect_rejects_mixed_dns_before_attempting_public_address(self):
        # An initial public answer must not hide a later private answer, even
        # when DNS changes after the URL's preflight validation.
        rows = public_dns('ignored', 443) + [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('::1', 443, 0, 0)),
        ]
        with mock.patch('server.audio.socket.getaddrinfo', return_value=rows), mock.patch('server.audio.socket.socket') as factory:
            with self.assertRaises(audio.AudioFetchError):
                audio._PublicHTTPSConnection('player.example.test')._create_connection(('player.example.test', 443), 10)
            factory.assert_not_called()

    def test_connection_failure_closes_socket_and_retries_checked_numeric_answer(self):
        rows = public_dns('ignored', 443) + [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.35', 443)),
        ]
        failed, working = mock.Mock(), mock.Mock()
        failed.connect.side_effect = OSError('synthetic connection failure')
        with mock.patch('server.audio.socket.getaddrinfo', return_value=rows) as dns, mock.patch('server.audio.socket.socket', side_effect=[failed, working]):
            result = audio._PublicHTTPSConnection('player.example.test')._create_connection(('player.example.test', 443), 10)
            self.assertIs(result, working)
            dns.assert_called_once()
            failed.connect.assert_called_once_with(('93.184.216.34', 443))
            failed.close.assert_called_once()
            working.connect.assert_called_once_with(('93.184.216.35', 443))
            working.close.assert_not_called()

    def test_default_fetch_ignores_environment_proxy_configuration(self):
        response = mock.MagicMock()
        response.headers.get_content_type.return_value = 'audio/mpeg'
        response.geturl.return_value = MEDIA
        response.__enter__.return_value = response
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.dict('os.environ', {'HTTPS_PROXY': 'http://127.0.0.1:9', 'ALL_PROXY': 'http://127.0.0.1:9'}), mock.patch('server.audio.socket.getaddrinfo', side_effect=public_dns), mock.patch('server.audio.build_opener', return_value=opener) as build:
            self.assertEqual(audio.default_fetch(MEDIA), (MEDIA, 'audio/mpeg', b''))
            handlers = build.call_args.args
            proxy_handlers = [h for h in handlers if isinstance(h, audio.ProxyHandler)]
            self.assertEqual(len(proxy_handlers), 1)
            self.assertEqual(proxy_handlers[0].proxies, {})
            self.assertTrue(any(isinstance(h, audio._PublicHTTPSHandler) for h in handlers))
            self.assertTrue(any(isinstance(h, audio._SafeRedirect) for h in handlers))

    def test_pinned_connection_preserves_tls_hostname_verification(self):
        connection = audio._PublicHTTPSConnection('player.example.test', timeout=10)
        self.assertTrue(connection._context.check_hostname)
        with mock.patch('server.audio.socket.getaddrinfo', side_effect=public_dns), mock.patch('server.audio.socket.socket') as factory, mock.patch.object(connection._context, 'wrap_socket') as wrap:
            connection.connect()
            wrap.assert_called_once_with(factory.return_value, server_hostname='player.example.test')
            factory.return_value.connect.assert_called_once_with(('93.184.216.34', 443))

    def test_cache_revalidates_embedded_media_dns(self):
        with mock.patch('server.audio.socket.getaddrinfo', side_effect=public_dns):
            result = resolve_audio(PAGE, fetch=fake('text/html', (HTML % MEDIA).encode()))
            self.assertEqual(result['status'], 'playable')
        def rebound(host, port, **kwargs):
            if host.startswith('media.'):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port))]
            return public_dns(host, port, **kwargs)
        with mock.patch('server.audio.socket.getaddrinfo', side_effect=rebound):
            result = resolve_audio(PAGE, fetch=fake('text/html', (HTML % MEDIA).encode()))
            self.assertEqual(result['status'], 'page_only')


if __name__ == '__main__':
    unittest.main()
