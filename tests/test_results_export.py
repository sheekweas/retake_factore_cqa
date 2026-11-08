"""Organizer submission contract using synthetic source data, no inference/network."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server.app import Application
from server.results_export import CRITERION_CODES, export_results, validate_submission
from server.storage import Store
from server.evaluation import PROMPT_REVISION


class ResultsExportTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / '.local' / 'tests'
        root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'results.sqlite3')
        self.app = Application(self.store)
        self.records = [{
            'conversation_id': f'synthetic-{i:03d}', 'language': ('RU', 'KZ', 'EN')[i % 3],
            'channel': 'agent chat' if i < 60 else 'voice call', 'audio_url': None,
            'transcript': [
                {'timestamp': '00:00', 'speaker': 'system', 'text': 'Synthetic start.'},
                {'timestamp': '00:01', 'speaker': 'bot', 'text': 'Synthetic routing.'},
                {'timestamp': '00:02', 'speaker': 'client', 'text': 'Synthetic request.'},
                {'timestamp': '00:03', 'speaker': 'operator', 'text': 'Synthetic answer.'},
            ]} for i in range(65)]
        self.app.dispatch('POST', '/api/imports', {'payload': self.records, 'group_id': 'DEV'})
        self.official = self.app.required('checklists', 'official-cqa-v1')
        self.evaluation = self.make_evaluation('synthetic-000')

    def make_evaluation(self, cid, model='synthetic-local-model', created='2026-11-08T07:00:00+00:00'):
        values = [True, None, None, 15, True, 7, True, False]
        answers = []
        for ident, value in zip(CRITERION_CODES, values):
            answer = {'criterion_id': ident, 'answer': value,
                      'explanation': 'Synthetic result rationale.',
                      'evidence_message_ids': [cid + (':2' if value is None else ':3')]}
            if value is None:
                answer['na_reason'] = 'Synthetic condition is inapplicable.'
            if ident == '08':
                answer.update(evidence_message_ids=[], missed=False,
                              checked_span={'start_message_id': cid + ':0', 'end_message_id': cid + ':3'})
            answers.append(answer)
        evaluation = {'id': cid + '-' + created, 'conversation_id': cid, 'checklist_id': self.official['id'],
                      'created_at': created, 'provider': {'type': 'ollama', 'model': model},
                      'original': {'answers': answers}, 'final': {'answers': deepcopy(answers)}}
        self.store.put('evaluations', evaluation['id'], evaluation)
        return evaluation

    def export(self, **options):
        return export_results(self.store, 'dev', 'DEV', 'Synthetic Test Team', **options)

    def test_export_api_uses_team_and_exact_current_provider(self):
        self.evaluation['provider'].update(model='synthetic-current', prompt_revision=PROMPT_REVISION, think=True)
        self.store.put('evaluations', self.evaluation['id'], self.evaluation)
        query = {'dataset': ['dev'], 'group_id': ['DEV'], 'team_name': ['Synthetic Test Team'], 'reviewed': ['0']}
        with patch.dict('os.environ', {'CQA_MODEL': 'synthetic-current', 'CQA_THINK': '1'}):
            payload = self.app.dispatch('GET', '/api/results/export', query=query)
        self.assertEqual(payload['team_name'], 'Synthetic Test Team')
        self.assertEqual(len(payload['results']), 65)
        self.assertEqual(payload['results'][0]['status'], 'ok')
        self.assertEqual(payload['results'][0]['score']['final_score'], 85.38)
        self.assertEqual(payload['model']['name'], 'synthetic-current')

    def test_export_api_rejects_missing_control_group_and_invalid_review_flag(self):
        for query in ({'dataset': ['prod'], 'team_name': ['Synthetic Test Team']},
                      {'team_name': ['Synthetic Test Team'], 'reviewed': ['2']}):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.app.dispatch('GET', '/api/results/export', query=query)

    def test_exact_mapping_source_indices_na_rounding_and_missing_errors(self):
        payload = self.export()
        self.assertEqual(payload['schema_version'], '1.0')
        self.assertEqual(len(payload['results']), 65)
        row = payload['results'][0]
        self.assertEqual(row['status'], 'ok')
        self.assertEqual([c['criterion_id'] for c in row['criteria']], list(CRITERION_CODES.values()))
        self.assertEqual(row['score'], {'final_score': 85.38, 'critical_flag': False})
        self.assertEqual(row['criteria'][0]['evidence_turns'], [4])
        self.assertEqual(row['criteria'][1]['evidence_turns'], [3])
        self.assertIn('N/A: Synthetic condition', row['criteria'][1]['rationale'])
        self.assertEqual(row['criteria'][7]['evidence_turns'], [1, 2, 3, 4])
        self.assertIn('реплики 1–4', row['criteria'][7]['rationale'])
        self.assertEqual(payload['results'][1], {
            'conversation_id': 'synthetic-001', 'status': 'error', 'criteria': [], 'score': None,
            'error': 'Не удалось получить действительную оценку официальным чек-листом.'})

    def test_original_is_default_and_reviewed_requires_explicit_opt_in(self):
        self.evaluation['final']['answers'][0]['answer'] = False
        self.store.put('evaluations', self.evaluation['id'], self.evaluation)
        self.assertEqual(self.export()['results'][0]['score']['final_score'], 85.38)
        self.assertEqual(self.export(reviewed=True)['results'][0]['score']['final_score'], 70)
        self.assertEqual(self.store.get('evaluations', self.evaluation['id']), self.evaluation)

    def test_critical_flag_does_not_zero_the_official_weighted_score(self):
        critical = self.evaluation['original']['answers'][7]
        critical.update(answer=True, evidence_message_ids=['synthetic-000:3'])
        critical.pop('checked_span')
        self.store.put('evaluations', self.evaluation['id'], self.evaluation)
        row = self.export()['results'][0]
        self.assertTrue(row['criteria'][7]['value'])
        self.assertEqual(row['score'], {'final_score': 85.38, 'critical_flag': True})

    def test_na_without_citations_is_not_a_processing_error_or_invented_evidence(self):
        for ident in ('02', '03'):
            answer = next(a for a in self.evaluation['original']['answers'] if a['criterion_id'] == ident)
            answer['evidence_message_ids'] = []
        self.store.put('evaluations', self.evaluation['id'], self.evaluation)
        row = self.export()['results'][0]
        self.assertEqual(row['status'], 'ok')
        self.assertEqual(row['score'], {'final_score': 85.38, 'critical_flag': False})
        for item in row['criteria'][1:3]:
            self.assertIsNone(item['value'])
            self.assertEqual(item['evidence_turns'], [])
            self.assertIn('N/A: Synthetic condition', item['rationale'])
        invalid = self.export()
        invalid['results'][0]['criteria'][0]['evidence_turns'] = []
        with self.assertRaises(ValueError):
            validate_submission(invalid, dataset='dev')

    def test_public_rationale_redacts_contacts_links_and_accounts_only(self):
        self.evaluation['original']['answers'][0]['explanation'] = (
            'Synthetic contact: https://example.invalid/private?sig=PRIVATE_SECRET '
            'client@example.invalid +7 (777) 123-45-67 KZ86125KZT5004100100. '
            'Первая реакция 7 секунд; проверены реплики 1–4.')
        self.evaluation['original']['answers'][1]['na_reason'] = 'Synthetic N/A: contact client@example.invalid.'
        self.store.put('evaluations', self.evaluation['id'], self.evaluation)
        row = self.export()['results'][0]
        self.assertEqual(row['status'], 'ok')
        self.assertEqual(row['score']['final_score'], 85.38)
        contact = row['criteria'][0]
        self.assertTrue(contact['value'])
        self.assertEqual(contact['evidence_turns'], [4])
        for private in ('PRIVATE_SECRET', 'client@example.invalid', '777', 'KZ86125KZT5004100100'):
            self.assertNotIn(private, repr(row))
        self.assertIn('7 секунд', contact['rationale'])
        self.assertIn('реплики 1–4', contact['rationale'])

    def test_latest_real_official_only_and_actual_multiple_model_names(self):
        newer = self.make_evaluation('synthetic-000', 'new-model', '2026-11-08T08:00:00+00:00')
        newer['original']['answers'][0]['answer'] = False
        self.store.put('evaluations', newer['id'], newer)
        self.make_evaluation('synthetic-001', 'second-model')
        injected = deepcopy(newer)
        injected.update(id='ignored-test-provider', created_at='9999', provider={'type': 'injected'})
        self.store.put('evaluations', injected['id'], injected)
        modified = deepcopy(self.official)
        modified.update(id='custom-copy', provenance='custom')
        modified['criteria'][0]['description'] = 'Different criterion.'
        self.store.put('checklists', modified['id'], modified)
        custom = deepcopy(newer)
        custom.update(id='ignored-custom', created_at='9999', checklist_id=modified['id'])
        self.store.put('evaluations', custom['id'], custom)
        payload = self.export()
        self.assertEqual(payload['results'][0]['score']['final_score'], 70)
        self.assertEqual(payload['model'], {'mode': 'local', 'name': 'new-model, second-model'})

    def test_missing_evidence_and_failed_attempt_are_errors_without_private_text(self):
        self.evaluation['original']['answers'][0]['evidence_message_ids'] = ['PRIVATE_BAD_ID']
        self.store.put('evaluations', self.evaluation['id'], self.evaluation)
        payload = self.export(errors={'synthetic-001': 'https://private.invalid/?sig=SECRET'})
        self.assertTrue(all(row['status'] == 'error' for row in payload['results']))
        self.assertNotIn('PRIVATE', repr(payload))
        self.assertNotIn('SECRET', repr(payload))
        with self.assertRaises(ValueError):
            self.export(require_complete=True)

    def test_provider_filter_never_substitutes_rejected_model_or_older_prompt(self):
        old = self.make_evaluation('synthetic-001', 'qwen2.5', '2026-11-08T09:00:00+00:00')
        old['provider'].update(prompt_revision='operator-evidence-v4', think=False)
        self.store.put('evaluations', old['id'], old)
        current = self.make_evaluation('synthetic-000', 'qwen3:8b', '2026-11-08T08:00:00+00:00')
        current['provider'].update(prompt_revision='operator-evidence-v7', think=True)
        self.store.put('evaluations', current['id'], current)
        provider_filter = {'model': 'qwen3:8b', 'prompt_revision': 'operator-evidence-v7', 'think': True}
        payload = self.export(provider_filter=provider_filter)
        self.assertEqual(payload['model']['name'], 'qwen3:8b')
        self.assertEqual(payload['results'][0]['status'], 'ok')
        self.assertEqual(payload['results'][1]['status'], 'error')
        for invalid in ({'endpoint': 'private'}, {'think': '1'}, {'model': ''}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.export(provider_filter=invalid)

    def external(self, cid, created='2026-11-08T10:00:00+00:00', **metadata):
        evaluation = self.make_evaluation(cid, 'gpt-6.1-sol', created)
        evaluation['provider'] = {
            'type': 'external_api', 'engine': 'codex-cli', 'model': 'gpt-6.1-sol',
            'prompt_revision': 'official-grounded-batch-v1', 'verified_run': True,
            'mode': 'external_api', 'run_id': 'synthetic-verified-run', **metadata,
        }
        self.store.put('evaluations', evaluation['id'], evaluation)
        return evaluation

    def test_external_is_ignored_by_default_and_explicit_mode_is_actual(self):
        external = self.external('synthetic-000')
        external['original']['answers'][0]['answer'] = False
        self.store.put('evaluations', external['id'], external)
        default = self.export()
        self.assertEqual(default['model'], {'mode': 'local', 'name': 'synthetic-local-model'})
        self.assertEqual(default['results'][0]['score']['final_score'], 85.38)
        selected = self.export(include_external=True)
        self.assertEqual(selected['model'], {'mode': 'external_api', 'name': 'gpt-6.1-sol'})
        self.assertEqual(selected['results'][0]['score']['final_score'], 70)
        self.assertFalse(selected['results'][0]['criteria'][0]['value'])

    def test_hybrid_uses_verified_external_alongside_filtered_local_model(self):
        self.evaluation['provider'].update(prompt_revision='operator-evidence-v7', think=True)
        self.store.put('evaluations', self.evaluation['id'], self.evaluation)
        self.external('synthetic-001')
        self.make_evaluation('synthetic-002', 'rejected-local-model')
        provider_filter = {'model': 'synthetic-local-model', 'prompt_revision': 'operator-evidence-v7', 'think': True}
        payload = self.export(include_external=True, provider_filter=provider_filter)
        self.assertEqual(payload['model'], {'mode': 'hybrid', 'name': 'gpt-6.1-sol, synthetic-local-model'})
        self.assertEqual([r['status'] for r in payload['results'][:3]], ['ok', 'ok', 'error'])
        self.assertEqual(payload['results'][1]['score']['final_score'], 85.38)
        self.assertEqual(payload['results'][1]['criteria'][3]['value'], 15)
        self.assertEqual(payload['results'][1]['criteria'][5]['value'], 7)

    def test_unverified_or_incompatible_external_provider_never_becomes_an_ok_result(self):
        invalid = [
            {'verified_run': False}, {'verified_run': 1}, {'engine': 'unverified-client'},
            {'prompt_revision': 'unknown-pipeline'}, {'type': 'injected'}, {'model': ''},
        ]
        for i, metadata in enumerate(invalid, 1):
            self.external(f'synthetic-{i:03d}', **metadata)
        payload = self.export(include_external=True)
        self.assertEqual(payload['model']['mode'], 'local')
        self.assertTrue(all(row['status'] == 'error' for row in payload['results'][1:7]))

    def test_latest_timestamp_selects_local_or_external_without_renaming_its_provider(self):
        self.external('synthetic-000', created='2026-11-08T06:00:00+00:00')
        payload = self.export(include_external=True)
        self.assertEqual(payload['model'], {'mode': 'local', 'name': 'synthetic-local-model'})
        self.assertEqual(payload['results'][0]['score']['final_score'], 85.38)

    def test_validator_rejects_bad_schema_types_steps_math_flags_duplicates_and_source_indices(self):
        original = self.export()
        corruptions = [
            lambda p: p.update(schema_version='2'),
            lambda p: p.update(team_name='Название команды'),
            lambda p: p['results'].append(deepcopy(p['results'][0])),
            lambda p: p['results'][0]['criteria'][0].update(value=1),
            lambda p: p['results'][0]['criteria'][0].update(value=None),
            lambda p: p['results'][0]['criteria'][3].update(value=14),
            lambda p: p['results'][0]['criteria'][5].update(value=True),
            lambda p: p['results'][0]['criteria'][0].update(evidence_turns=[0]),
            lambda p: p['results'][0]['criteria'][0].update(evidence_turns=[5]),
            lambda p: p['results'][0]['criteria'][0].update(evidence_turns=[True]),
            lambda p: p['results'][0]['score'].update(final_score=85.37),
            lambda p: p['results'][0]['score'].update(critical_flag=True),
            lambda p: p['results'][1].update(score={'final_score': 0}),
        ]
        for corrupt in corruptions:
            payload = deepcopy(original)
            corrupt(payload)
            with self.subTest(corrupt=corrupt), self.assertRaises(ValueError):
                validate_submission(payload, dataset='dev', conversations=self.store.list('conversations'))

    def test_prod_requires_ten_real_chat_ids_and_never_invents_missing_ids(self):
        with self.assertRaises(ValueError):
            export_results(self.store, 'prod', 'MISSING', 'Synthetic Test Team', model_name='actual-attempted-model')
        records = deepcopy(self.records[:10])
        for i, record in enumerate(records):
            record['conversation_id'] = f'control-synthetic-{i}'
        self.app.dispatch('POST', '/api/imports', {'payload': records, 'group_id': 'CONTROL'})
        payload = export_results(self.store, 'prod', 'CONTROL', 'Synthetic Test Team', model_name='actual-attempted-model')
        self.assertEqual(len(payload['results']), 10)
        self.assertTrue(all(r['status'] == 'error' for r in payload['results']))
        validate_submission(payload, dataset='prod', expected_ids=[r['conversation_id'] for r in records])
        with self.assertRaises(ValueError):
            validate_submission(payload, expected_ids=['different-source-id'])


if __name__ == '__main__':
    unittest.main()
