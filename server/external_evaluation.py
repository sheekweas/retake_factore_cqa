"""Validate completed external model runs before atomically saving new history.

The caller must first verify the real CLI process succeeded and retain its raw
output privately. This internal ingestion helper neither invokes a model nor
accepts model-supplied totals, and never changes conversation source snapshots.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import uuid

from server.evaluation import score, validate_checklist, validate_evidence_roles
from server.presets import official_checklist

CHECKLIST_ID = 'official-cqa-v1'


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + ' must be a nonempty string')
    return value


def ingest_external_batch(store, evaluations, model_name, run_id, expected_ids,
                          group_id='DEV', prompt_revision='official-grounded-batch-v1',
                          engine='codex-cli'):
    """Save an exact, validated batch as immutable new external evaluations.

    Every row is {conversation_id, answers}. All validation and JSON encoding
    finish before the single transactional put_many call, so one invalid row
    rejects the whole batch and older evaluations remain untouched.
    """
    _required_text(model_name, 'model_name')
    _required_text(run_id, 'run_id')
    _required_text(group_id, 'group_id')
    _required_text(prompt_revision, 'prompt_revision')
    _required_text(engine, 'engine')
    if isinstance(expected_ids, (str, bytes)):
        raise ValueError('expected_ids must be a nonempty collection of unique IDs')
    try:
        expected = list(expected_ids)
    except TypeError:
        raise ValueError('expected_ids must be a nonempty collection of unique IDs') from None
    if (not expected or any(not isinstance(cid, str) or not cid.strip() for cid in expected)
            or len(set(expected)) != len(expected)):
        raise ValueError('expected_ids must be a nonempty collection of unique IDs')
    if not isinstance(evaluations, list) or not evaluations:
        raise ValueError('evaluations must be a nonempty list')
    if any(not isinstance(row, dict) or set(row) != {'conversation_id', 'answers'} for row in evaluations):
        raise ValueError('each external evaluation requires only conversation_id and answers')
    ids = [row['conversation_id'] for row in evaluations]
    if any(not isinstance(cid, str) for cid in ids) or len(set(ids)) != len(ids) or set(ids) != set(expected):
        raise ValueError('external batch IDs must exactly match the requested unique IDs')

    checklist = store.get('checklists', CHECKLIST_ID)
    if checklist is None:
        raise ValueError('official checklist is not available')
    canonical = validate_checklist(official_checklist())
    checklist = validate_checklist(checklist)
    if (checklist.get('id') != CHECKLIST_ID or checklist.get('provenance') != 'official'
            or checklist['criteria'] != canonical['criteria'] or checklist['rules'] != canonical['rules']):
        raise ValueError('saved official checklist differs from canonical rules')

    provider = {'type': 'external_api', 'engine': engine, 'model': model_name,
                'prompt_revision': prompt_revision, 'verified_run': True,
                'run_id': run_id, 'mode': 'external_api'}
    created = datetime.now(timezone.utc).isoformat()
    ready = []
    for row in evaluations:
        cid = row['conversation_id']
        conversation = store.get('conversations', cid)
        if (conversation is None or conversation.get('conversation_id') != cid
                or conversation.get('dataset_group_id') != group_id):
            raise ValueError('external conversation must exist in the requested dataset group')
        answers = deepcopy(row['answers'])
        result = score(checklist, answers, conversation)
        validate_evidence_roles(checklist, answers, conversation)
        identifier = str(uuid.uuid4())
        if store.get('evaluations', identifier) is not None:
            raise ValueError('new evaluation identifier is already in use')
        original = {'answers': answers, 'result': result}
        evaluation = {'id': identifier, 'conversation_id': cid,
                      'checklist_id': CHECKLIST_ID, 'created_at': created,
                      'original': original, 'final': deepcopy(original),
                      'provider': deepcopy(provider)}
        # Check the complete persisted shape, including any unsupported values,
        # before initiating the transaction for the whole batch.
        json.dumps(evaluation, ensure_ascii=False, allow_nan=False)
        ready.append(evaluation)
    store.put_many([('evaluations', item['id'], item) for item in ready])
    return deepcopy(ready)
