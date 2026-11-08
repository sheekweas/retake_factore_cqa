"""Resolve a conversation's recording link to directly playable media.

DEV recording links are HTML player pages (no CORS) that embed a signed media
URL in ``<audio><source src>``. The browser cannot read those pages, so the
server fetches the page (bounded size/time), extracts the media URL and returns
it; the browser then plays it through a plain cross-origin ``<audio src>``.
URLs are secrets (signed): they are never logged and errors never echo them.
"""
from __future__ import annotations

import threading
import time
import ipaddress
import socket
from http.client import HTTPSConnection
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

TIMEOUT_SECONDS = 10
MAX_BYTES = 256 * 1024
CACHE_TTL_SECONDS = 600
CACHE_MAX_ITEMS = 256
LOCAL_HOSTS = {'localhost', '127.0.0.1', '::1'}


class AudioFetchError(Exception):
    """Redacted fetch failure; message never contains the URL."""


def _allowed(url, allow_http_localhost=False):
    try:
        if not isinstance(url, str) or '\\' in url or any(ord(c) < 33 or ord(c) == 127 for c in url):
            return False
        parts = urlsplit(url)
        if not parts.hostname or parts.username is not None or parts.password is not None:
            return False
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        if allow_http_localhost and parts.scheme == 'http' and parts.hostname in LOCAL_HOSTS:
            rows = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
            return bool(rows) and all(ipaddress.ip_address(row[4][0]).is_loopback for row in rows)
        if parts.scheme != 'https':
            return False
        _public_addresses(parts.hostname, port)
        return True
    except (ValueError, OSError, AudioFetchError):
        return False


def _public_addresses(host, port):
    """Reject mixed public/private DNS answers as well as nonpublic literals."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and (not literal.is_global or literal.is_multicast or literal.is_reserved):
        raise AudioFetchError('Недопустимый адрес записи')
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise AudioFetchError('Адрес записи недоступен')
    for row in addresses:
        address = ipaddress.ip_address(row[4][0])
        if not address.is_global or address.is_multicast or address.is_reserved or address.is_unspecified:
            raise AudioFetchError('Недопустимый адрес записи')
    return addresses


class _PublicHTTPSConnection(HTTPSConnection):
    """Pin a checked DNS answer while retaining the original TLS hostname/SNI."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = self._connect_public

    @staticmethod
    def _connect_public(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        rows = _public_addresses(*address)
        last_error = None
        for family, kind, protocol, _, target in rows:
            sock = socket.socket(family, kind, protocol)
            try:
                if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                sock.connect(target)  # numeric sockaddr: no second DNS lookup/rebinding
                return sock
            except OSError as error:
                last_error = error
                sock.close()
        raise last_error or AudioFetchError('Не удалось подключиться к записи')


class _PublicHTTPSHandler(HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPSConnection, req, context=self._context)


class _SafeRedirect(HTTPRedirectHandler):
    def __init__(self, allow_http_localhost):
        self.allow_http_localhost = allow_http_localhost

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed(newurl, self.allow_http_localhost):
            raise AudioFetchError('Недопустимое перенаправление ссылки на запись')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_fetch(url, allow_http_localhost=False):
    """Return (final_url, content_type, body). Body is skipped for media, capped otherwise."""
    try:
        if not _allowed(url, allow_http_localhost):
            raise AudioFetchError('Недопустимый адрес записи')
        # Do not let environment proxies bypass checked public destinations.
        opener = build_opener(ProxyHandler({}), _PublicHTTPSHandler(), _SafeRedirect(allow_http_localhost))
        request = Request(url, headers={'User-Agent': 'CQA-audio-resolver/1', 'Accept': 'text/html,audio/*;q=0.9,*/*;q=0.1'})
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            content_type = response.headers.get_content_type()
            final_url = response.geturl()
            if content_type.startswith(('audio/', 'video/')):
                return final_url, content_type, b''
            return final_url, content_type, response.read(MAX_BYTES)
    except AudioFetchError:
        raise
    except Exception as error:  # network, TLS, HTTP status, timeout
        if hasattr(error, 'close'):
            error.close()  # HTTPError holds an open response
        raise AudioFetchError(type(error).__name__) from None


class _AudioSourceParser(HTMLParser):
    """Collect the first <audio src> or <source src> inside the first matching <audio>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.found = None  # (src, type)

    def handle_starttag(self, tag, attrs):
        if self.found:
            return
        values = {k: (v or '').strip() for k, v in attrs}
        if tag == 'audio':
            self.depth += 1
            if values.get('src'):
                self.found = (values['src'], values.get('type') or None)
        elif tag == 'source' and self.depth and values.get('src'):
            self.found = (values['src'], values.get('type') or None)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == 'audio' and self.depth:
            self.depth -= 1


def extract_audio_source(html, base_url):
    parser = _AudioSourceParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return None
    if not parser.found:
        return None
    src, media_type = parser.found
    return urljoin(base_url, src), media_type


_cache = {}
_cache_lock = threading.Lock()


def clear_cache():
    with _cache_lock:
        _cache.clear()


def _result(status, message, media_url=None, media_type=None, page_url=None):
    return {'status': status, 'media_url': media_url, 'media_type': media_type, 'page_url': page_url, 'message': message}


def _resolve(url, fetch, allow_http_localhost):
    try:
        final_url, content_type, body = fetch(url)
    except Exception:
        return _result('error', 'Не удалось получить страницу записи (сеть или таймаут). Используйте ссылку на оригинал.', page_url=url)
    content_type = (content_type or '').split(';')[0].strip().lower()
    final_url = final_url or url
    if not _allowed(final_url, allow_http_localhost):
        return _result('error', 'Недопустимый адрес страницы записи.')
    if content_type.startswith('audio/'):
        return _result('playable', 'Ссылка ведёт непосредственно на аудиофайл.', media_url=final_url, media_type=content_type, page_url=url)
    if content_type in ('text/html', 'application/xhtml+xml'):
        raw = body[:MAX_BYTES] if isinstance(body, (bytes, bytearray)) else str(body or '').encode('utf-8')[:MAX_BYTES]
        found = extract_audio_source(raw.decode('utf-8', errors='replace'), final_url)
        if not found:
            return _result('page_only', 'На странице записи не найден аудиоплеер; откройте оригинал по ссылке.', page_url=url)
        media_url, media_type = found
        if not _allowed(media_url, allow_http_localhost):
            return _result('page_only', 'Источник аудио на странице имеет недопустимую схему; откройте оригинал по ссылке.', page_url=url)
        if media_type and not media_type.lower().startswith('audio/'):
            media_type = None
        return _result('playable', 'Аудиофайл извлечён со страницы записи.', media_url=media_url, media_type=media_type, page_url=url)
    return _result('page_only', 'Ссылка не является аудиофайлом или страницей с плеером; откройте оригинал по ссылке.', page_url=url)


def resolve_audio(audio_url, fetch=None, allow_http_localhost=False, use_cache=True):
    """Never raises. Returns {status, media_url, media_type, page_url, message}."""
    try:
        if not audio_url:
            return _result('none', 'Ссылка на запись отсутствует в данных.')
        if not isinstance(audio_url, str) or not _allowed(audio_url, allow_http_localhost):
            return _result('error', 'Ссылка на запись имеет недопустимую схему (разрешена только https).')
        if fetch is None:
            def fetch(url):
                return default_fetch(url, allow_http_localhost)
        now = time.monotonic()
        cache_key = (audio_url, allow_http_localhost)
        if use_cache:
            with _cache_lock:
                hit = _cache.get(cache_key)
                if hit and now - hit[0] < CACHE_TTL_SECONDS:
                    if not hit[1].get('media_url') or _allowed(hit[1]['media_url'], allow_http_localhost):
                        return dict(hit[1])
        result = _resolve(audio_url, fetch, allow_http_localhost)
        if use_cache and result['status'] != 'error':
            with _cache_lock:
                if len(_cache) >= CACHE_MAX_ITEMS:
                    _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
                _cache[cache_key] = (now, dict(result))
        return result
    except Exception:
        return _result('error', 'Внутренняя ошибка при разборе ссылки на запись.')
