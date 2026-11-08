"""External-result ingestion tests: synthetic answers only, no model invocation."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server.app import Application
from server.data import normalize_dataset
from server.external_evaluation import ingest_external_batch
from server.storage import Store


class ExternalEvaluationTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / '.local/tests'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'synthetic.sqlite3'
        self.store = Store(self.path)
        Application(self.store)
        self.ids = ['external-synthetic-1', 'external-synthetic-2']
        self.sources = normalize_dataset([{
            'conversation_id': cid, 'channel': 'agent chat', 'language': 'EN',
            'audio_url': None, 'transcript': [
                {'timestamp': '00:00', 'speaker': 'client', 'text': 'Synthetic general question.'},
                {'timestamp': '00:03', 'speaker': 'operator', 'text': 'Hello, I am Test. Synthetic answer. Anything else? Goodbye.'},
                {'timestamp': '00:04', 'speaker': 'system', 'text': 'Synthetic conversation closed.'},
            ],
        } for cid in self.ids], group_id='DEV')
        self.store.put_many([('conversations', c['conversation_id'], c) for c in self.sources])
        self.rows = []
        for source in self.sources:
            cid = source['conversation_id']
            answers = []
            for ident in ['01', '02', '03', '04', '05', '06', '07', '08']:
                value = {'02': None, '03': None, '04': 20, '06': 10, '08': False}.get(ident, True)
                answer = {'criterion_id': ident, 'answer': value,
                          'explanation': 'Synthetic external ingestion test, not model accuracy.',
                          'evidence_message_ids': [cid + ':1'], 'missed': False, 'gross_error': False}
                if value is None:
                    answer['na_reason'] = 'Synthetic initial request contains enough public information.'
                    answer['evidence_message_ids'] = [cid + ':0']
                if ident == '08':
                    answer.update(evidence_message_ids=[], checked_span={
                        'start_message_id': cid + ':0', 'end_message_id': cid + ':2'})
                answers.append(answer)
            self.rows.append({'conversation_id': cid, 'answers': answers})

    def ingest(self, rows=None, expected=None, **kwargs):
        return ingest_external_batch(self.store, self.rows if rows is None else rows,
                                     'synthetic-model-only', 'synthetic-verified-run',
                                     self.ids if expected is None else expected, **kwargs)

    def test_valid_batch_has_real_provider_metadata_and_recomputed_scores_preserving_sources_history(self):
        older = {'id': 'older-synthetic', 'conversation_id': self.ids[0],
                 'original': {'preserved': True}, 'final': {'preserved': True}}
        self.store.put('evaluations', older['id'], older)
        with patch.object(self.store, 'put_many', wraps=self.store.put_many) as write:
            saved = self.ingest()
        self.assertEqual(write.call_count, 1)
        self.assertEqual(len(saved), 2)
        for record in saved:
            self.assertEqual(record['original'], record['final'])
            self.assertEqual(record['final']['result']['total'], 100)
            self.assertFalse(record['final']['result']['gross_error'])
            self.assertEqual(record['provider'], {
                'type': 'external_api', 'engine': 'codex-cli', 'model': 'synthetic-model-only',
                'prompt_revision': 'official-grounded-batch-v1', 'verified_run': True,
                'run_id': 'synthetic-verified-run', 'mode': 'external_api',
            })
            self.assertEqual(Store(self.path).get('evaluations', record['id']), record)
        self.assertEqual(self.store.get('evaluations', older['id']), older)
        self.assertEqual(self.store.list('conversations'), self.sources)
        saved[0]['final']['result']['total'] = 0
        self.assertEqual(self.store.get('evaluations', saved[0]['id'])['original']['result']['total'], 100)

    def test_exact_unique_ids_required(self):
        for rows, expected in [(self.rows[:1], self.ids),
                               ([self.rows[0], self.rows[0]], self.ids),
                               (self.rows, [self.ids[0], self.ids[0]]),
                               (self.rows, self.ids + ['unknown-synthetic']),
                               ([], self.ids)]:
            with self.subTest(rows=len(rows), expected=expected), self.assertRaises(ValueError):
                self.ingest(rows, expected)
        self.assertEqual(self.store.list('evaluations'), [])

    def test_invalid_last_row_rejects_whole_batch_before_any_write(self):
        rows = deepcopy(self.rows)
        rows[1]['answers'][3]['answer'] = 21
        with patch.object(self.store, 'put_many', wraps=self.store.put_many) as write:
            with self.assertRaises(ValueError):
                self.ingest(rows)
        write.assert_not_called()
        self.assertEqual(self.store.list('evaluations'), [])

    def test_unknown_source_and_wrong_group_are_rejected(self):
        rows = deepcopy(self.rows)
        rows[1]['conversation_id'] = 'unknown-synthetic'
        with self.assertRaises(ValueError):
            self.ingest(rows, [self.ids[0], 'unknown-synthetic'])
        source = deepcopy(self.sources[1])
        source['dataset_group_id'] = 'OTHER'
        self.store.put('conversations', source['conversation_id'], source)
        with self.assertRaises(ValueError):
            self.ingest()
        self.assertEqual(self.store.list('evaluations'), [])

    def test_invalid_evidence_roles_ids_duplicate_criteria_and_scale_steps_rejected(self):
        mutations = [
            lambda a: a[0].update(evidence_message_ids=[self.ids[0] + ':0']),
            lambda a: a[0].update(evidence_message_ids=['unknown-message']),
            lambda a: a[1].update(criterion_id='01'),
            lambda a: a[5].update(answer=8),
            lambda a: a[6].update(answer=None),
        ]
        for mutation in mutations:
            rows = deepcopy(self.rows)
            mutation(rows[0]['answers'])
            with self.subTest(mutation=mutations.index(mutation)), self.assertRaises(ValueError):
                self.ingest(rows)
        self.assertEqual(self.store.list('evaluations'), [])

    def test_model_totals_and_modified_official_rules_never_accepted(self):
        rows = deepcopy(self.rows)
        rows[0]['total'] = 100
        with self.assertRaises(ValueError):
            self.ingest(rows)
        checklist = self.store.get('checklists', 'official-cqa-v1')
        checklist['criteria'][0]['weight'] = 11
        self.store.put('checklists', 'official-cqa-v1', checklist)
        with self.assertRaises(ValueError):
            self.ingest()
        self.assertEqual(self.store.list('evaluations'), [])

    def test_metadata_and_answer_shape_must_be_valid_before_save(self):
        for field, value in [('model_name', ''), ('run_id', None), ('group_id', '')]:
            args = {'model_name': 'synthetic', 'run_id': 'synthetic-run', 'group_id': 'DEV'}
            args[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                ingest_external_batch(self.store, self.rows, expected_ids=self.ids, **args)
        rows = deepcopy(self.rows)
        rows[1]['answers'] = {}
        with self.assertRaises(ValueError):
            self.ingest(rows)
        self.assertEqual(self.store.list('evaluations'), [])


if __name__ == '__main__':
    unittest.main()
