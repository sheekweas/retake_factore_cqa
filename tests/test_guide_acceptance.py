"""Guide acceptance: configurable criteria, versioned results and prompt authority.

Synthetic data and mocked transport only; these tests do not measure LLM accuracy.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from server.app import Application
from server.data import normalize_dataset
from server.evaluation import CUSTOM_PROMPT_REVISION, PROMPT_REVISION, infer, prompt_revision_for
from server.presets import official_checklist
from server.storage import Store


class GuideAcceptanceTests(unittest.TestCase):
    def setUp(self):
        runtime = Path(__file__).resolve().parents[1] / '.local' / 'tests'
        runtime.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=runtime)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'guide.sqlite3'
        self.conversation = normalize_dataset([{
            'conversation_id': 'guide-synthetic', 'language': 'EN',
            'channel': 'agent chat', 'audio_url': None,
            'transcript': [
                {'speaker': 'client', 'timestamp': '00:00', 'text': 'Synthetic request.'},
                {'speaker': 'operator', 'timestamp': '00:03', 'text': 'Goodbye.'},
            ],
        }])[0]

    def provider_request(self, checklist):
        answers = [{
            'criterion_id': c['id'],
            'answer': c['max'] if c['type'] == 'numeric' else c['id'] != '08',
            'explanation': 'Synthetic protocol fixture, not a semantic assertion.',
            'evidence_message_ids': ['guide-synthetic:1'],
        } for c in checklist['criteria']]
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({
            'done': True, 'response': json.dumps({'answers': answers}),
        }).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch.dict(os.environ, {
            'CQA_MODEL': 'synthetic-test-only', 'CQA_THINK': '0',
            'CQA_OLLAMA_URL': 'http://127.0.0.1:11434',
        }), patch('server.evaluation.build_opener', return_value=opener):
            infer(checklist, self.conversation)
        return json.loads(opener.open.call_args.args[0].data)

    def test_replaced_completion_rule_is_not_overridden_by_official_hint(self):
        checklist = official_checklist()
        checklist['provenance'] = 'custom'
        closing = next(c for c in checklist['criteria'] if c['id'] == '07')
        closing.update(
            title='Replacement completion rule',
            description='Yes if the operator says goodbye. A summary or offer of further help is not required.',
            allow_na=False,
        )
        closing.pop('na_condition', None)
        payload = self.provider_request(checklist)
        self.assertIn(closing['description'], payload['prompt'])
        self.assertNotIn('a farewell alone is No', payload['prompt'])
        self.assertNotIn('No unless the operator ALSO summarised', payload['prompt'])
        for stale_hint in (
            'For need-identification criteria, when',
            "including its greeting and self-introduction in the client's language",
            'A polite operator facing no client complaint satisfies the empathy criterion',
            'consultation N/A condition holds: return null',
            'response timing matters only as an explicit waiting period',
        ):
            with self.subTest(stale_hint=stale_hint):
                self.assertNotIn(stale_hint, payload['prompt'])

    def test_new_numeric_scale_reaches_model_without_code_changes(self):
        checklist = {
            'name': 'Synthetic custom rubric', 'provenance': 'custom',
            'criteria': [{
                'id': 'replacement-scale', 'title': 'Replacement scale',
                'description': 'Choose the configured step only.',
                'type': 'numeric', 'weight': 7, 'min': 2, 'max': 8,
                'allowed_values': [2, 4, 8], 'allow_na': False,
                'scale_labels': {'2': 'No usable answer', '4': 'Partial', '8': 'Complete'},
            }],
        }
        payload = self.provider_request(checklist)
        fields = payload['format']['properties']['answers']['properties']
        self.assertEqual(set(fields), {'replacement-scale'})
        self.assertEqual(fields['replacement-scale']['anyOf'][0]['properties']['answer']['enum'], [2, 4, 8])
        self.assertIn('Choose the configured step only.', payload['prompt'])
        self.assertIn('No usable answer', payload['prompt'])

    def test_prompt_provenance_distinguishes_replaced_rules_from_official_rubric(self):
        official = official_checklist()
        self.assertEqual(prompt_revision_for(official), PROMPT_REVISION)
        renamed = deepcopy(official)
        renamed.update(name='Display-only rename', id='synthetic-saved-version', version=23)
        self.assertEqual(prompt_revision_for(renamed), PROMPT_REVISION)
        for field, value in [('description', 'New organiser criterion.'), ('weight', 11),
                             ('title', 'Changed organiser criterion')]:
            changed = deepcopy(official)
            changed['criteria'][0][field] = value
            with self.subTest(field=field):
                self.assertEqual(prompt_revision_for(changed), CUSTOM_PROMPT_REVISION)

    def test_custom_version_na_scale_review_and_original_survive_reopen(self):
        seen = []

        def provider(checklist, conversation):
            seen.append(deepcopy(checklist))
            return [{
                'criterion_id': c['id'], 'answer': c.get('max'),
                'explanation': 'Synthetic custom-rule integration.',
                'evidence_message_ids': [conversation['transcript'][1]['id']],
                **({'na_reason': 'Synthetic optional criterion does not apply.'}
                   if c['type'] == 'boolean' else {}),
            } for c in checklist['criteria']]

        app = Application(Store(self.path), provider=provider)
        app.dispatch('POST', '/api/imports', {'payload': [self.conversation]})
        rubric = {
            'name': 'Synthetic changed rubric', 'provenance': 'custom',
            'criteria': [
                {'id': 'new-scale', 'title': 'New scale', 'section': 'Resolution',
                 'type': 'numeric', 'weight': 2, 'min': 2, 'max': 8,
                 'allowed_values': [2, 4, 8], 'allow_na': False},
                {'id': 'optional', 'title': 'Optional check', 'section': 'Context',
                 'type': 'boolean', 'weight': 3, 'allow_na': True,
                 'na_condition': 'Optional check does not apply in this synthetic case.'},
            ],
        }
        v1 = app.dispatch('POST', '/api/checklists', rubric)
        updated = deepcopy(v1)
        updated['criteria'][0]['weight'] = 9
        v2 = app.dispatch('POST', '/api/checklists', updated)
        self.assertEqual(v2['version'], 2)
        self.assertEqual(app.required('checklists', v1['id'])['criteria'][0]['weight'], 2)
        ev = app.dispatch('POST', '/api/evaluations', {
            'conversation_id': self.conversation['conversation_id'], 'checklist_id': v2['id'],
        })
        self.assertEqual(seen, [v2])
        self.assertEqual(ev['final']['result']['total'], 100)
        self.assertEqual(ev['final']['result']['applicable_weight'], 9)
        appeal = app.dispatch('POST', '/api/appeals', {
            'evaluation_id': ev['id'], 'criterion_id': 'new-scale',
            'comment': 'Synthetic partial-answer review.',
        })
        answer = deepcopy(ev['final']['answers'][0])
        answer['answer'] = 4
        reviewed = app.dispatch('POST', '/api/appeals/' + appeal['id'] + '/review', {
            'decision': 'change', 'reason': 'Synthetic partial score rationale.', 'answer': answer,
        })
        final = reviewed['evaluation']
        self.assertEqual(final['original'], ev['original'])
        self.assertEqual(final['final']['result']['total'], 33.33)
        self.assertEqual(final['checklist_id'], v2['id'])
        reopened = Application(Store(self.path))
        self.assertEqual(reopened.required('evaluations', ev['id']), final)
        self.assertEqual(reopened.required('appeals', appeal['id'])['review']['reason'],
                         'Synthetic partial score rationale.')


if __name__ == '__main__':
    unittest.main()
