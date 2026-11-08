"""Single-origin JSON API; no credentials or dialogue text in request logs."""
from __future__ import annotations

import argparse
import copy
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from server.audio import resolve_audio
from server.data import fetch_dataset, normalize_dataset
from server.evaluation import prompt_revision_for, generation_limit, infer, score, think_enabled, validate_checklist, validate_evidence_roles
from server.presets import official_checklist
from server.storage import Store
from server.transcriptions import attach_transcription
from server.results_export import export_results

ROOT = Path(__file__).resolve().parent.parent


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def identifier():
    return str(uuid.uuid4())


def now():
    return datetime.now(timezone.utc).isoformat()


class Application:
    def __init__(self, store, provider=infer):
        self.store, self.provider = store, provider
        self.lock = threading.RLock()
        self.inference_lock = threading.Lock()
        preset = validate_checklist(official_checklist())
        preset.update(id='official-cqa-v1', family_id='official-cqa', version=1, created_at=now())
        self.store.put_if_absent('checklists', preset['id'], preset)

    def appeal_view(self, appeal):
        """Include conversation linkage on both new and pre-upgrade appeals."""
        result = copy.deepcopy(appeal)
        if 'conversation_id' not in result:
            result['conversation_id'] = self.required('evaluations', result['evaluation_id'])['conversation_id']
        return result

    def required(self, collection, key):
        if not isinstance(key, str) or not key.strip():
            raise ValueError('Требуется непустой строковый ID')
        item = self.store.get(collection, key)
        if item is None:
            raise APIError(404, 'not_found', 'Объект не найден')
        return item

    def dispatch(self, method, path, body=None, query=None):
        body = {} if body is None else body
        query = query or {}
        parts = [unquote(p) for p in path.strip('/').split('/')]
        if method == 'POST' and parts == ['api', 'evaluations']:
            return self.evaluate(body)
        if method == 'GET' and len(parts) == 4 and parts[:2] == ['api', 'conversations'] and parts[3] == 'audio':
            # Read-only network resolution runs outside the write lock (up to ~10 s).
            return resolve_audio(self.required('conversations', parts[2]).get('audio_url'))
        # Serialize writes, including review read/modify/write, to prevent lost updates.
        with self.lock:
            return self._dispatch(method, parts, body, query)

    def evaluate(self, body):
        # Serial local inference must not hold the application lock: navigation,
        # evidence reads and review of other evaluations remain available.
        with self.inference_lock:
            with self.lock:
                c = self.required('conversations', body.get('conversation_id'))
                checklist = self.required('checklists', body.get('checklist_id'))
                provider = self.provider
                provenance = ({'type': 'ollama', 'model': os.environ.get('CQA_MODEL', '').strip(),
                               'prompt_revision': prompt_revision_for(checklist), 'think': think_enabled()}
                              if provider is infer else {'type': 'injected'})
                if provider is infer and os.environ.get('CQA_KV_CACHE_TYPE'):
                    provenance['runtime'] = {'kv_cache_type': os.environ['CQA_KV_CACHE_TYPE'],
                                             'flash_attention': os.environ.get('CQA_FLASH_ATTENTION') == '1',
                                             'num_predict': generation_limit()}
            # Inputs are independent immutable storage snapshots. New versions
            # receive new IDs; imports cannot replace an existing conversation.
            answers = provider(checklist, c)
            result = score(checklist, answers, c)
            original = {'answers': copy.deepcopy(answers), 'result': result}
            evaluation = {'id': identifier(), 'conversation_id': c['conversation_id'],
                          'checklist_id': checklist['id'], 'created_at': now(),
                          'original': original, 'final': copy.deepcopy(original),
                          'provider': provenance}
            with self.lock:
                self.store.put('evaluations', evaluation['id'], evaluation)
            return evaluation

    def _dispatch(self, method, p, body, query):
        if p == ['api', 'health'] and method == 'GET':
            return {'status': 'ok', 'api_version': 1, 'model_configured': bool(os.getenv('CQA_MODEL'))}
        if p == ['api', 'results', 'export'] and method == 'GET':
            dataset = query.get('dataset', ['dev'])[0]
            group = query.get('group_id', ['DEV' if dataset == 'dev' else ''])[0]
            team = query.get('team_name', [''])[0]
            reviewed = query.get('reviewed', ['0'])[0]
            complete = query.get('require_complete', ['0'])[0]
            external = query.get('include_external', ['1' if dataset == 'dev' else '0'])[0]
            if any(value not in ('0', '1') for value in (reviewed, complete, external)):
                raise ValueError('reviewed, require_complete и include_external должны быть 0 или 1')
            return export_results(
                self.store, dataset, group, team, reviewed=reviewed == '1',
                require_complete=complete == '1', model_name=os.getenv('CQA_MODEL', '').strip() or None,
                provider_filter={'model': os.getenv('CQA_MODEL', '').strip(),
                                 'prompt_revision': prompt_revision_for(official_checklist()),
                                 'think': think_enabled()},
                include_external=external == '1',
            )
        if p == ['api', 'imports'] and method == 'POST':
            source = body.get('source', 'json')
            if source == 'dev':
                conversations = fetch_dataset(body.get('group_id', 'DEV'))
            elif source == 'json':
                conversations = normalize_dataset(body.get('payload'), body.get('group_id', 'JSON'))
            else:
                raise ValueError('source: json или dev')
            # Source snapshots never change underneath stored evaluation evidence.
            for c in conversations:
                existing = self.store.get('conversations', c['conversation_id'])
                if existing is not None and existing != c:
                    raise APIError(409, 'immutable_conversation', 'ID уже занят другим диалогом; используйте новый ID')
            self.store.put_many([('conversations', c['conversation_id'], c) for c in conversations])
            return {'imported': len(conversations), 'conversation_ids': [c['conversation_id'] for c in conversations]}
        if p == ['api', 'conversations'] and method == 'GET':
            items = self.store.list('conversations')
            for field in ('language', 'channel', 'dataset_group_id'):
                if field in query:
                    items = [c for c in items if c.get(field) == query[field][0]]
            return {'items': [{k: v for k, v in c.items() if k != 'transcript'} | {'message_count': len(c['transcript'])} for c in items]}
        if len(p) == 3 and p[:2] == ['api', 'conversations'] and method == 'GET':
            return attach_transcription(self.required('conversations', p[2]), self.store)
        if p == ['api', 'checklists']:
            if method == 'GET':
                return {'items': self.store.list('checklists')}
            if method == 'POST':
                checklist = validate_checklist(body)
                if checklist['provenance'] == 'official':
                    # Editing a source preset creates a user configuration; it must
                    # not continue to claim exact correspondence with the PDF.
                    metadata = {'id', 'family_id', 'version', 'created_at'}
                    config = {k: v for k, v in checklist.items() if k not in metadata}
                    if config != validate_checklist(official_checklist()):
                        checklist['provenance'] = 'custom'
                family = body.get('family_id') or identifier()
                if not isinstance(family, str) or not family.strip():
                    raise ValueError('family_id должен быть строкой')
                previous = [c for c in self.store.list('checklists') if c['family_id'] == family]
                checklist.update(id=identifier(), family_id=family, version=max([c['version'] for c in previous], default=0) + 1, created_at=now())
                self.store.put('checklists', checklist['id'], checklist)
                return checklist
        if len(p) == 3 and p[:2] == ['api', 'checklists'] and method == 'GET':
            return self.required('checklists', p[2])
        if p == ['api', 'evaluations']:
            if method == 'GET':
                items = self.store.list('evaluations')
                if 'conversation_id' in query:
                    items = [e for e in items if e['conversation_id'] == query['conversation_id'][0]]
                return {'items': items}
        if len(p) == 3 and p[:2] == ['api', 'evaluations'] and method == 'GET':
            return self.required('evaluations', p[2])
        if p == ['api', 'appeals']:
            if method == 'GET':
                items = [self.appeal_view(a) for a in self.store.list('appeals')]
                if 'evaluation_id' in query:
                    items = [a for a in items if a['evaluation_id'] == query['evaluation_id'][0]]
                return {'items': items}
            if method == 'POST':
                e = self.required('evaluations', body.get('evaluation_id'))
                criterion_id = body.get('criterion_id')
                disputed = next((a for a in e['final']['answers'] if a['criterion_id'] == criterion_id), None)
                if disputed is None:
                    raise ValueError('Неизвестный критерий')
                comment = body.get('comment')
                if not isinstance(comment, str) or not comment.strip():
                    raise ValueError('Комментарий обязателен')
                if any(a['evaluation_id'] == e['id'] and a['criterion_id'] == criterion_id and a['status'] == 'pending' for a in self.store.list('appeals')):
                    raise APIError(409, 'pending_appeal', 'Апелляция уже ожидает решения')
                appeal = {'id': identifier(), 'evaluation_id': e['id'], 'conversation_id': e['conversation_id'], 'criterion_id': criterion_id, 'comment': comment, 'status': 'pending', 'created_at': now(), 'disputed_answer': copy.deepcopy(disputed), 'original_result': copy.deepcopy(e['original']['result'])}
                self.store.put('appeals', appeal['id'], appeal)
                return appeal
        if len(p) == 3 and p[:2] == ['api', 'appeals'] and method == 'GET':
            return self.appeal_view(self.required('appeals', p[2]))
        if len(p) == 4 and p[:2] == ['api', 'appeals'] and p[3] == 'review' and method == 'POST':
            appeal = self.appeal_view(self.required('appeals', p[2]))
            if appeal['status'] != 'pending':
                raise APIError(409, 'already_reviewed', 'Решение уже сохранено')
            reason = body.get('reason')
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError('Причина решения обязательна')
            decision = body.get('decision')
            if decision not in ('confirm', 'change'):
                raise ValueError('decision: confirm или change')
            e = self.required('evaluations', appeal['evaluation_id'])
            answers = copy.deepcopy(e['final']['answers'])
            if decision == 'change':
                replacement = body.get('answer')
                if not isinstance(replacement, dict) or replacement.get('criterion_id') != appeal['criterion_id']:
                    raise ValueError('Требуется полный answer спорного критерия')
                answers = [replacement if a['criterion_id'] == appeal['criterion_id'] else a for a in answers]
            checklist = self.required('checklists', e['checklist_id'])
            conversation = self.required('conversations', e['conversation_id'])
            final = {'answers': answers, 'result': score(checklist, answers, conversation)}
            if decision == 'change':
                # Apply current source-role rules to the new finding only. A
                # confirmation must preserve historical evidence, and unrelated
                # legacy rows must not prevent a reviewer from correcting one.
                validate_evidence_roles(checklist, [replacement], conversation)
            appeal.update(status='reviewed', review={'decision': decision, 'reason': reason, 'reviewed_at': now(), 'answer': body.get('answer') if decision == 'change' else None, 'previous_result': e['final']['result'], 'final_result': final['result']})
            e['final'] = final
            # Store supports a transactional batch for atomic result + decision persistence.
            self.store.put_many([('evaluations', e['id'], e), ('appeals', appeal['id'], appeal)])
            return {'appeal': appeal, 'evaluation': e}
        raise APIError(404, 'not_found', 'Маршрут не найден')


def make_server(application, host='127.0.0.1', port=8000):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def parse_request(self):
            if not super().parse_request():
                return False
            # Binding to loopback is not enough against DNS rebinding: a browser
            # can reach 127.0.0.1 while retaining an attacker's Host/Origin. Check
            # Host before routing *any* HTTP method, including static files and
            # unsupported methods. Multiple Host headers are ambiguous and denied.
            hosts = self.headers.get_all('Host', [])
            port = self.server.server_port
            allowed = {f'localhost:{port}', f'127.0.0.1:{port}'}
            if port == 80:
                allowed.update({'localhost', '127.0.0.1'})
            if len(hosts) != 1 or hosts[0].lower() not in allowed:
                self.respond(403, {'error': {'code': 'host', 'message': 'Недопустимый Host'}})
                return False
            return True

        def respond(self, status, data):
            raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(raw)

        def handle_request(self):
            try:
                parsed = urlsplit(self.path)
                if not parsed.path.startswith('/api/') and self.command == 'GET':
                    return self.static(parsed.path)
                if self.command == 'POST':
                    # JSON-only plus same-origin check limits cross-site writes to local data.
                    origin = self.headers.get('Origin')
                    if origin and origin != 'http://' + self.headers.get('Host', ''):
                        raise APIError(403, 'origin', 'Недопустимый Origin')
                    if self.headers.get_content_type() != 'application/json':
                        raise APIError(415, 'content_type', 'Ожидается application/json')
                    length = int(self.headers.get('Content-Length', '0'))
                    if length <= 0 or length > 20_000_000:
                        raise APIError(413, 'body_size', 'Недопустимый размер JSON')
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError('Ожидается JSON объект')
                else:
                    body = None
                result = application.dispatch(self.command, parsed.path, body, parse_qs(parsed.query))
                self.respond(200, result)
            except APIError as e:
                self.respond(e.status, {'error': {'code': e.code, 'message': e.message}})
            except (ValueError, TypeError, KeyError) as e:
                # Provider/data exceptions must themselves be redacted by their adapters.
                self.respond(422, {'error': {'code': 'validation_error', 'message': str(e)}})
            except Exception:
                self.respond(500, {'error': {'code': 'internal_error', 'message': 'Внутренняя ошибка сервера'}})

        def static(self, path):
            base = (ROOT / 'web').resolve()
            target = (base / unquote(path).lstrip('/')).resolve()
            if path == '/':
                target = base / 'index.html'
            if not target.is_relative_to(base) or not target.is_file():
                return self.respond(404, {'error': {'code': 'ui_pending', 'message': 'Интерфейс ещё не установлен; API: /api/health'}})
            import mimetypes
            raw = target.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', (mimetypes.guess_type(target.name)[0] or 'application/octet-stream') + '; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(raw)

        do_GET = handle_request
        do_POST = handle_request

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    store = Store(ROOT / '.local' / 'cqa.sqlite3')
    server = make_server(Application(store), port=args.port)
    print(f'CQA: http://127.0.0.1:{args.port} (Ctrl+C to stop)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
