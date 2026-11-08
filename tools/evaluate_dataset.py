"""Resumable serial evaluation of imported data through the real local API.

No scores are fabricated. Progress and errors stay in .local; saved evaluations
remain authoritative. Stop files are checked between calls, never by killing an
in-flight model request. Remove the stop file before resuming.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from server.evaluation import PROMPT_REVISION, validate_checklist
from server.presets import official_checklist

CHECKLIST_ID = 'official-cqa-v1'
MAX_RESPONSE = 20 * 1024 * 1024


def stamp():
    return datetime.now(timezone.utc).isoformat()


def local_path(value):
    path = Path(value)
    path = (ROOT / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(ROOT / '.local'):
        raise ValueError('progress and stop files must stay inside project .local')
    return path


class BatchError(Exception):
    """Only fixed privacy-safe messages are exposed in progress or console."""


class API:
    def __init__(self, base_url, timeout=660):
        parts = urlsplit(base_url)
        if (parts.scheme != 'http' or parts.hostname not in ('127.0.0.1', 'localhost')
                or parts.username or parts.password or parts.path not in ('', '/')
                or parts.query or parts.fragment):
            raise ValueError('API must be a local HTTP origin')
        self.base = base_url.rstrip('/')
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}))

    def request(self, route, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.base + '/api/' + route, data=data,
                          headers={'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=self.timeout if body is not None else 30) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise BatchError('Ответ локального сервиса превышает допустимый размер.')
            return json.loads(raw)
        except HTTPError as error:
            if error.code == 422:
                raise BatchError('Сервис отклонил оценку: модель недоступна, истёк тайм-аут или ответ не прошёл проверку.') from None
            if error.code == 404:
                raise BatchError('Диалог или официальный чек-лист не найден.') from None
            raise BatchError(f'Локальный сервис вернул HTTP {error.code}.') from None
        except (OSError, HTTPException):
            raise BatchError('Локальный сервис не ответил в пределах времени запроса или недоступен.') from None
        except (ValueError, UnicodeError):
            raise BatchError('Локальный сервис вернул недопустимый JSON.') from None


def provider_matches(evaluation, expected):
    provider = evaluation.get('provider') or {}
    return (evaluation.get('checklist_id') == CHECKLIST_ID
            and type(provider.get('think')) is bool
            and all(provider.get(key) == value for key, value in expected.items())
            and isinstance(evaluation.get('original'), dict)
            and isinstance(evaluation.get('final'), dict))


def find_current(api, cid, expected):
    response = api.request('evaluations?' + urlencode({'conversation_id': cid}))
    matches = [e for e in response['items'] if e.get('conversation_id') == cid and provider_matches(e, expected)]
    return max(matches, key=lambda e: (e.get('created_at', ''), e['id'])) if matches else None


def summarize(progress):
    rows = list(progress['conversations'].values())
    return {'total': len(rows), 'ok': sum(r['status'] == 'ok' for r in rows),
            'failed': sum(r['status'] == 'error' for r in rows),
            'pending': sum(r['status'] == 'pending' for r in rows)}


def save_progress(path, progress):
    progress['updated_at'] = stamp()
    progress['summary'] = summarize(progress)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(progress, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    # Windows readers and antivirus can briefly hold the destination without
    # delete sharing. Keep atomic replacement, retrying only that short lock.
    for attempt in range(7):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt == 6:
                raise BatchError('Не удалось сохранить прогресс: локальный файл занят.') from None
            time.sleep(0.025 * 2 ** attempt)


def safe_id(cid):
    return re.sub(r'[^A-Za-z0-9_.:-]', '_', cid)[:100]


def run_batch(args, api=None):
    api = api or API(args.base_url, args.timeout)
    progress_path, stop_path = local_path(args.progress_file), local_path(args.stop_file)
    expected = {'type': 'ollama', 'model': args.model,
                'prompt_revision': args.prompt_revision, 'think': args.think == '1'}
    canonical = validate_checklist(official_checklist())
    chosen = validate_checklist(api.request('checklists/' + CHECKLIST_ID))
    if chosen['criteria'] != canonical['criteria'] or chosen['rules'] != canonical['rules']:
        raise BatchError('Сохранённый официальный чек-лист отличается от исходной конфигурации.')
    query = {'dataset_group_id': args.group_id}
    if args.language:
        query['language'] = args.language
    records = api.request('conversations?' + urlencode(query))['items']
    # Shorter chats first helps complete more records before a fixed deadline;
    # voice calls remain included and use exactly the same official rubric.
    records.sort(key=lambda c: (c.get('channel') == 'voice call', c.get('message_count', 0), c['conversation_id']))
    ids = [c['conversation_id'] for c in records]
    if not ids or len(ids) != len(set(ids)):
        raise BatchError('Нет подходящих диалогов или их ID повторяются.')
    old = {}
    if progress_path.is_file():
        try:
            previous = json.loads(progress_path.read_text(encoding='utf-8'))
            if previous.get('dataset_group_id') == args.group_id and previous.get('provider') == expected:
                old = previous.get('conversations', {})
        except (ValueError, OSError):
            pass
    progress = {'schema_version': '1.0', 'dataset_group_id': args.group_id,
                'checklist_id': CHECKLIST_ID, 'provider': expected, 'state': 'running',
                'created_at': stamp(), 'conversations': {cid: {'status': 'pending'} for cid in ids}}
    save_progress(progress_path, progress)
    attempts = 0
    try:
        for cid in ids:
            if stop_path.exists():
                progress['state'] = 'paused'
                break
            started = time.monotonic()
            row = progress['conversations'][cid]
            try:
                # History is checked even for a previously failed request: the
                # server may have completed after a client-side timeout.
                existing = find_current(api, cid, expected)
                if existing:
                    row.update(status='ok', evaluation_id=existing['id'], source='existing')
                elif old.get(cid, {}).get('status') == 'error' and not args.retry_errors:
                    row.update(status='error', error=old[cid].get('error', 'Предыдущая попытка завершилась ошибкой.'))
                elif args.limit is not None and attempts >= args.limit:
                    continue
                else:
                    attempts += 1
                    print(json.dumps({'event': 'start', 'conversation_id': safe_id(cid), 'attempt': attempts}), flush=True)
                    try:
                        evaluation = api.request('evaluations', {'conversation_id': cid, 'checklist_id': CHECKLIST_ID})
                    except BatchError:
                        recovered = find_current(api, cid, expected)
                        if recovered is None:
                            raise
                        evaluation = recovered
                    if evaluation.get('conversation_id') != cid or not provider_matches(evaluation, expected):
                        raise BatchError('Параметры сохранённой оценки не совпадают с конфигурацией пакета.')
                    row.update(status='ok', evaluation_id=evaluation['id'], source='generated')
            except BatchError as error:
                row.update(status='error', error=str(error))
            row['elapsed_seconds'] = round(time.monotonic() - started, 2)
            save_progress(progress_path, progress)
            print(json.dumps({'event': row['status'], 'conversation_id': safe_id(cid),
                              'elapsed_seconds': row['elapsed_seconds']}), flush=True)
        if progress['state'] == 'running':
            progress['state'] = 'finished' if not summarize(progress)['pending'] else 'limited'
    except KeyboardInterrupt:
        progress['state'] = 'interrupted'
        save_progress(progress_path, progress)
        raise
    save_progress(progress_path, progress)
    print(json.dumps({'event': progress['state'], **progress['summary']}), flush=True)
    return progress


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--group-id', default='DEV')
    parser.add_argument('--language', choices=('RU', 'KZ', 'EN'))
    parser.add_argument('--model', default=os.getenv('CQA_MODEL', 'qwen3:8b'))
    parser.add_argument('--prompt-revision', default=PROMPT_REVISION)
    parser.add_argument('--think', choices=('0', '1'), default=os.getenv('CQA_THINK', '1'))
    parser.add_argument('--timeout', type=float, default=660)
    parser.add_argument('--limit', type=int, help='Maximum NEW model calls; remaining IDs stay pending')
    parser.add_argument('--retry-errors', action='store_true')
    parser.add_argument('--progress-file', default='.local/evaluation-progress.json')
    parser.add_argument('--stop-file', default='.local/STOP_EVALUATION')
    args = parser.parse_args(argv)
    if not args.group_id.strip() or not args.model.strip() or not args.prompt_revision.strip():
        parser.error('group, model and prompt revision must be nonempty')
    if args.think not in ('0', '1'):
        parser.error('think must be 0 or 1')
    if not 1 <= args.timeout <= 3600 or args.limit is not None and args.limit < 1:
        parser.error('timeout must be 1..3600 seconds and limit must be positive')
    return args


def main(argv=None):
    try:
        progress = run_batch(parse_args(argv))
        return int(bool(progress['summary']['failed'] or progress['summary']['pending']))
    except KeyboardInterrupt:
        print('{"event":"interrupted"}', flush=True)
        return 130
    except BatchError as error:
        print(json.dumps({'event': 'failed', 'error': str(error)}, ensure_ascii=False), flush=True)
        return 1
    except Exception as error:
        # Do not print HTTP bodies, signed URLs, transcript text or tracebacks.
        print(json.dumps({'event': 'failed', 'error_type': type(error).__name__}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
