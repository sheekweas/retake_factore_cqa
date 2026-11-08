"""UI test fixture server — ТЕСТОВЫЙ ПРОВАЙДЕР, только для проверки интерфейса.

Not a working evaluation: answers are generated deterministically from the transcript
structure (no language understanding) so the browser UI can be exercised without a model.
Uses a separate database `.local/ui-fixture/fixture.sqlite3` and imports only the synthetic
examples (`examples/conversations.json`, демонстрационный пример). Never use for real scores.

Run:  python web/dev/fixture_server.py --port 8031 [--fresh]
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.app import Application, make_server  # noqa: E402
from server.storage import Store  # noqa: E402


def fixture_provider(checklist, conversation):
    """Deterministic structural answers. ТЕСТОВЫЙ ПРОВАЙДЕР — not model output."""
    msgs = conversation['transcript']
    ids = [m['id'] for m in msgs]
    op = [m['id'] for m in msgs if m.get('speaker') == 'operator'] or ids[:1]
    answers = []
    na_used = False
    for i, c in enumerate(checklist['criteria']):
        base = {'criterion_id': c['id'], 'explanation': f'ТЕСТОВЫЙ ПРОВАЙДЕР (не модель): структурный ответ для «{c["title"]}».'}
        if c.get('allow_na') and not na_used and i > 0:
            na_used = True
            answers.append(base | {'answer': None, 'evidence_message_ids': [], 'na_reason': 'ТЕСТОВЫЙ ПРОВАЙДЕР: критерий помечен N/A для проверки интерфейса.'})
            continue
        if c['type'] == 'boolean':
            if i % 3 == 2:
                answers.append(base | {'answer': False, 'evidence_message_ids': [], 'missed': True,
                                       'checked_span': {'start_message_id': ids[0], 'end_message_id': ids[-1]}})
            else:
                answers.append(base | {'answer': True, 'evidence_message_ids': op[:2]})
        else:
            lo, hi = c['min'], c['max']
            answers.append(base | {'answer': hi if i % 2 == 0 else lo, 'evidence_message_ids': op[-1:]})
    return answers


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--port', type=int, default=8031)
    p.add_argument('--fresh', action='store_true')
    args = p.parse_args()
    runtime = ROOT / '.local' / 'ui-fixture'
    if args.fresh and runtime.exists():
        shutil.rmtree(runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    app = Application(Store(runtime / 'fixture.sqlite3'), provider=fixture_provider)
    examples = json.loads((ROOT / 'examples' / 'conversations.json').read_text(encoding='utf-8'))
    app.dispatch('POST', '/api/imports', {'source': 'json', 'payload': examples, 'group_id': 'JSON'})
    if not app.dispatch('GET', '/api/checklists')['items']:
        app.dispatch('POST', '/api/checklists', json.loads((ROOT / 'examples' / 'checklist.json').read_text(encoding='utf-8')))
    server = make_server(app, port=args.port)
    print(f'UI FIXTURE (ТЕСТОВЫЙ ПРОВАЙДЕР): http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
