"""Synthetic API tests. демонстрационный пример, не официальный чек-лист."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest

from server.evaluation import CUSTOM_PROMPT_REVISION
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server.app import Application, make_server
from server.storage import Store

DEMO = {
    'name': 'демонстрационный пример, не официальный чек-лист',
    'provenance': 'demonstration',
    'criteria': [{'id': 'demo', 'title': 'Демонстрационный критерий', 'type': 'boolean', 'weight': 1, 'allow_na': True}],
    'rules': {'decimals': 2, 'gross_error_policy': 'flag'},
}
CONVERSATION = {'conversation_id': 'test-ru', 'language': 'RU', 'channel': 'agent chat', 'audio_url': None, 'transcript': [{'speaker': 'operator', 'timestamp': '00:01', 'text': 'Тестовая реплика, синтетический пример.'}]}


def test_answer(value=True):
    return {'criterion_id': 'demo', 'answer': value, 'explanation': 'Только тест', 'evidence_message_ids': ['test-ru:0']}


class APITest(unittest.TestCase):
    def setUp(self):
        runtime = Path(__file__).resolve().parents[1] / '.local' / 'tests'
        runtime.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=runtime)
        self.path = Path(self.tmp.name) / 'test.sqlite3'
        self.app = Application(Store(self.path), provider=lambda *_: [test_answer()])
        self.server = make_server(self.app, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, path, data=None, headers=None):
        req = Request(self.base + path, data=json.dumps(data).encode() if data is not None else None, headers={'Content-Type': 'application/json'} | (headers or {}))
        try:
            with urlopen(req, timeout=5) as r:
                return r.status, json.load(r)
        except HTTPError as e:
            with e:
                return e.code, json.load(e)

    def seed(self):
        status, imported = self.request('/api/imports', {'source': 'json', 'payload': [CONVERSATION]})
        self.assertEqual(status, 200, imported)
        status, checklist = self.request('/api/checklists', DEMO)
        self.assertEqual(status, 200, checklist)
        return checklist

    def test_end_to_end_review_and_reopen(self):
        checklist = self.seed()
        status, evaluation = self.request('/api/evaluations', {'conversation_id': 'test-ru', 'checklist_id': checklist['id']})
        self.assertEqual(status, 200, evaluation)
        self.assertEqual(evaluation['original']['result']['total'], 100)
        original = copy.deepcopy(evaluation['original'])
        status, appeal = self.request('/api/appeals', {'evaluation_id': evaluation['id'], 'criterion_id': 'demo', 'comment': 'Синтетическая апелляция'})
        self.assertEqual(status, 200, appeal)
        self.assertEqual(appeal['conversation_id'], 'test-ru')
        self.assertEqual(self.request('/api/appeals/' + appeal['id']), (200, appeal))
        status, review = self.request('/api/appeals/' + appeal['id'] + '/review', {'decision': 'change', 'reason': 'Тест пересмотра', 'answer': test_answer(False)})
        self.assertEqual(status, 200, review)
        self.assertEqual(review['evaluation']['original'], original)
        self.assertEqual(review['evaluation']['final']['result']['total'], 0)
        saved = Application(Store(self.path)).required('evaluations', evaluation['id'])
        self.assertEqual(saved, review['evaluation'])
        self.assertEqual(self.request('/api/appeals/' + appeal['id'] + '/review', {'decision': 'confirm', 'reason': 'Повтор'})[0], 409)

    def test_official_seed_survives_restart_and_custom_edit(self):
        status, listing = self.request('/api/checklists')
        self.assertEqual(status, 200)
        self.assertEqual(len(listing['items']), 1)
        preset = listing['items'][0]
        self.assertEqual(preset['provenance'], 'official')
        self.assertEqual([c['id'] for c in preset['criteria']], ['01', '02', '03', '04', '05', '06', '07', '08'])
        self.assertEqual(sum(c['weight'] for c in preset['criteria']), 100)
        Application(Store(self.path))
        self.assertEqual(self.request('/api/checklists')[1]['items'], [preset])
        changed = copy.deepcopy(preset)
        changed['criteria'][0]['title'] = 'Пользовательская формулировка'
        status, custom = self.request('/api/checklists', changed)
        self.assertEqual(status, 200, custom)
        self.assertEqual(custom['version'], 2)
        self.assertEqual(custom['family_id'], preset['family_id'])
        self.assertEqual(custom['provenance'], 'custom')
        self.assertEqual(self.request('/api/checklists/' + preset['id'])[1], preset)
        Application(Store(self.path))
        self.assertEqual(len(self.request('/api/checklists')[1]['items']), 2)

    def test_legacy_appeal_get_enriches_without_rewriting(self):
        checklist = self.seed()
        _, evaluation = self.request('/api/evaluations', {'conversation_id': 'test-ru', 'checklist_id': checklist['id']})
        _, appeal = self.request('/api/appeals', {'evaluation_id': evaluation['id'], 'criterion_id': 'demo', 'comment': 'Тест'})
        del appeal['conversation_id']
        self.app.store.put('appeals', appeal['id'], appeal)
        status, detail = self.request('/api/appeals/' + appeal['id'])
        self.assertEqual(status, 200)
        self.assertEqual(detail['conversation_id'], 'test-ru')
        self.assertEqual(self.request('/api/appeals')[1]['items'], [detail])
        self.assertNotIn('conversation_id', self.app.store.get('appeals', appeal['id']))
        self.assertEqual(self.request('/api/appeals/missing')[0], 404)

    def test_official_numeric_review_rejects_in_between_steps_atomically(self):
        self.seed()
        preset = self.request('/api/checklists/official-cqa-v1')[1]
        def official_answers(checklist, _conversation):
            return [dict(criterion_id=c['id'], answer=(c['max'] if c['type'] == 'numeric' else c['id'] != '08'),
                         explanation='Синтетический тест официальной шкалы', evidence_message_ids=['test-ru:0'])
                    for c in checklist['criteria']]
        self.app.provider = official_answers
        status, evaluation = self.request('/api/evaluations', {'conversation_id': 'test-ru', 'checklist_id': preset['id']})
        self.assertEqual(status, 200, evaluation)
        original = copy.deepcopy(evaluation['original'])
        self.assertEqual(original['result']['total'], 100)
        for criterion_id, invalid, valid, expected_total in [('04', 12, 15, 95), ('06', 8, 7, 90.5)]:
            _, appeal = self.request('/api/appeals', {'evaluation_id': evaluation['id'], 'criterion_id': criterion_id, 'comment': 'Проверка ступеней'})
            answer = next(copy.deepcopy(a) for a in original['answers'] if a['criterion_id'] == criterion_id)
            answer['answer'] = invalid
            request = {'decision': 'change', 'reason': 'Проверка шкалы', 'answer': answer}
            self.assertEqual(self.request('/api/appeals/' + appeal['id'] + '/review', request)[0], 422)
            self.assertEqual(self.request('/api/appeals/' + appeal['id'])[1]['status'], 'pending')
            self.assertEqual(self.request('/api/evaluations/' + evaluation['id'])[1], evaluation)
            answer['answer'] = valid
            status, reviewed = self.request('/api/appeals/' + appeal['id'] + '/review', request)
            self.assertEqual(status, 200, reviewed)
            evaluation = reviewed['evaluation']
            self.assertEqual(evaluation['original'], original)
            self.assertEqual(evaluation['final']['result']['total'], expected_total)
        _, appeal = self.request('/api/appeals', {'evaluation_id': evaluation['id'], 'criterion_id': '08', 'comment': 'Проверка грубой ошибки'})
        answer = next(copy.deepcopy(a) for a in original['answers'] if a['criterion_id'] == '08')
        answer['answer'] = True
        answer['gross_error'] = False
        status, reviewed = self.request('/api/appeals/' + appeal['id'] + '/review', {'decision': 'change', 'reason': 'Флаг из ответа', 'answer': answer})
        self.assertEqual(status, 200, reviewed)
        self.assertTrue(reviewed['evaluation']['final']['result']['gross_error'])
        self.assertEqual(reviewed['evaluation']['final']['result']['total'], 90.5)
        self.assertEqual(reviewed['evaluation']['original'], original)

    def test_versions_and_immutable_imports(self):
        checklist = self.seed()
        status, version2 = self.request('/api/checklists', DEMO | {'family_id': checklist['family_id'], 'name': 'Новая версия'})
        self.assertEqual(status, 200, version2)
        self.assertEqual(version2['version'], 2)
        self.assertEqual(self.request('/api/checklists/' + checklist['id'])[1]['version'], 1)
        changed = copy.deepcopy(CONVERSATION)
        changed['transcript'][0]['text'] = 'Изменено'
        self.assertEqual(self.request('/api/imports', {'payload': [changed]})[0], 409)
        self.assertEqual(self.request('/api/conversations/test-ru')[1]['transcript'][0]['timestamp'], '00:01')

    def test_official_inference_validation_and_exact_rounding_persist(self):
        self.seed()
        preset = self.request('/api/checklists/official-cqa-v1')[1]
        answers = [dict(criterion_id=c['id'], answer=False if c['type'] == 'boolean' else 0,
                        explanation='Synthetic arithmetic integration test',
                        evidence_message_ids=['test-ru:0']) for c in preset['criteria']]
        self.app.provider = lambda *_: copy.deepcopy(answers)
        payload = {'conversation_id': 'test-ru', 'checklist_id': preset['id']}
        for index, invalid in [(3, 12), (5, 8)]:
            answers[index]['answer'] = invalid
            status, error = self.request('/api/evaluations', payload)
            self.assertEqual(status, 422, error)
            self.assertEqual(self.request('/api/evaluations')[1]['items'], [])
            answers[index]['answer'] = 0
        # 10.5 earned / 80 applicable * 100 = 13.125, HALF_UP => 13.13.
        answers[3].update(answer=None, na_reason='Чат закончился до формулировки запроса (синтетический тест).')
        answers[5]['answer'] = 7
        answers[7].update(answer=True, gross_error=False)
        status, evaluation = self.request('/api/evaluations', payload)
        self.assertEqual(status, 200, evaluation)
        self.assertEqual(evaluation['final']['result']['total'], 13.13)
        self.assertEqual(evaluation['final']['result']['applicable_weight'], 80)
        self.assertTrue(evaluation['final']['result']['gross_error'])
        self.assertEqual(Application(Store(self.path)).required('evaluations', evaluation['id']), evaluation)

    def test_provider_snapshot_survives_configuration_change_review_and_reopen(self):
        checklist = self.seed()
        with patch('server.app.infer', return_value=[test_answer()]) as transport, \
             patch.dict('os.environ', {'CQA_MODEL': 'synthetic-model-v1',
                                       'CQA_THINK': '0',
                                       'CQA_OLLAMA_URL': 'http://PRIVATE_ENDPOINT_SENTINEL'}):
            self.app.provider = transport
            status, evaluation = self.request('/api/evaluations', {
                'conversation_id': 'test-ru', 'checklist_id': checklist['id']})
        self.assertEqual(status, 200, evaluation)
        self.assertEqual(evaluation['provider'], {'type': 'ollama', 'model': 'synthetic-model-v1',
                                                'prompt_revision': CUSTOM_PROMPT_REVISION, 'think': False})
        self.assertNotIn('PRIVATE_ENDPOINT_SENTINEL', json.dumps(evaluation))
        _, appeal = self.request('/api/appeals', {'evaluation_id': evaluation['id'],
            'criterion_id': 'demo', 'comment': 'Synthetic provider history check'})
        with patch.dict('os.environ', {'CQA_MODEL': 'synthetic-model-v2'}):
            status, reviewed = self.request('/api/appeals/' + appeal['id'] + '/review', {
                'decision': 'change', 'reason': 'Synthetic review', 'answer': test_answer(False)})
        self.assertEqual(status, 200, reviewed)
        self.assertEqual(reviewed['evaluation']['provider'], evaluation['provider'])
        self.assertEqual(reviewed['evaluation']['original'], evaluation['original'])
        reopened = Application(Store(self.path)).required('evaluations', evaluation['id'])
        self.assertEqual(reopened, reviewed['evaluation'])

    def test_no_model_and_origin_errors(self):
        checklist = self.seed()
        def no_model(*_):
            raise ValueError('модель не настроена')
        self.app.provider = no_model
        status, body = self.request('/api/evaluations', {'conversation_id': 'test-ru', 'checklist_id': checklist['id']})
        self.assertEqual(status, 422)
        self.assertEqual(body['error']['message'], 'модель не настроена')
        self.assertEqual(self.request('/api/checklists', DEMO, {'Origin': 'https://unrelated.example'})[0], 403)
        self.assertEqual(self.request('/api/evaluations')[1]['items'], [])

    def test_invalid_review_does_not_mutate(self):
        checklist = self.seed()
        _, evaluation = self.request('/api/evaluations', {'conversation_id': 'test-ru', 'checklist_id': checklist['id']})
        _, appeal = self.request('/api/appeals', {'evaluation_id': evaluation['id'], 'criterion_id': 'demo', 'comment': 'Тест'})
        bad = test_answer(False) | {'evidence_message_ids': ['nonexistent']}
        self.assertEqual(self.request('/api/appeals/' + appeal['id'] + '/review', {'decision': 'change', 'reason': 'Тест', 'answer': bad})[0], 422)
        self.assertEqual(self.request('/api/evaluations/' + evaluation['id'])[1], evaluation)
        self.assertEqual(self.request('/api/appeals')[1]['items'][0]['status'], 'pending')

    def test_confirm_and_duplicate_pending_appeal(self):
        checklist = self.seed()
        _, evaluation = self.request('/api/evaluations', {'conversation_id': 'test-ru', 'checklist_id': checklist['id']})
        payload = {'evaluation_id': evaluation['id'], 'criterion_id': 'demo', 'comment': 'Проверка'}
        _, appeal = self.request('/api/appeals', payload)
        self.assertEqual(self.request('/api/appeals', payload)[0], 409)
        self.assertEqual(self.request('/api/appeals/' + appeal['id'] + '/review', {'decision': 'confirm', 'reason': ''})[0], 422)
        status, review = self.request('/api/appeals/' + appeal['id'] + '/review', {'decision': 'confirm', 'reason': 'Оснований для изменения нет'})
        self.assertEqual(status, 200, review)
        self.assertEqual(review['evaluation'], evaluation)
        self.assertEqual(review['appeal']['review']['decision'], 'confirm')


if __name__ == '__main__':
    unittest.main()
