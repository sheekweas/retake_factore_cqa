"""Submission JSON 1.0 from immutable source conversations and real evaluations."""
from fractions import Fraction
import math
import os
import re

from server.evaluation import score, validate_checklist, validate_evidence_roles
from server.presets import official_checklist


CRITERION_CODES = dict(zip(('01', '02', '03', '04', '05', '06', '07', '08'),
                          ('contact', 'discovery', 'identification', 'consultation',
                           'empathy', 'dialogue', 'closure', 'critical')))
WEIGHTS = dict(zip(CRITERION_CODES.values(), (10, 15, 20, 20, 15, 15, 5, 0)))
NA_CODES = {'discovery', 'identification', 'consultation', 'closure'}
NUMERIC_STEPS = {'consultation': (0, 5, 10, 15, 20), 'dialogue': (0, 4, 7, 10)}
COUNTS = {'dev': 65, 'prod': 10}


def _public_rationale(value):
    """Remove contact details and signed links without changing grading fields."""
    value = re.sub(r'(?i)\b(?:https?://|www\.)[^\s<>"\']+', '[скрыто]', value)
    value = re.sub(r'(?i)(?<![\w.+-])[\w.+-]+@[\w.-]+\.[a-zа-я]{2,}', '[скрыто]', value)
    value = re.sub(r'\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b', '[скрыто]', value)
    value = re.sub(r'\b[A-Z]{2}\d{2}(?: [A-Z0-9]{4}){3,7}(?: [A-Z0-9]{1,4})?\b', '[скрыто]', value)
    value = re.sub(r'\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,})\b', '[скрыто]', value)
    value = re.sub(r'(?i)\b(?:api[_ -]?key|token|password|пароль|токен)\s*[:=]\s*\S+', '[скрыто]', value)
    def phone(match):
        return '[скрыто]' if sum(c.isdigit() for c in match[0]) >= 8 else match[0]
    return re.sub(r'(?<!\w)\+?\d(?:[\d ()\-./]*\d)?(?!\w)', phone, value)


def _text(value, label, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(label + ': required nonempty string')
    return value.strip()


def _model_name(value):
    value = _text(value, 'model.name', 500)
    if any(c in value for c in ('\n', '\r', '?', '&', '=')) or '://' in value:
        raise ValueError('model.name must contain model names only')
    return value


def _expected_score(criteria):
    total, applicable = Fraction(0), 0
    for item in criteria:
        code, value = item['criterion_id'], item['value']
        if value is None:
            continue
        weight = WEIGHTS[code]
        applicable += weight
        maximum = NUMERIC_STEPS[code][-1] if code in NUMERIC_STEPS else 1
        total += Fraction(int(value), maximum) * weight
    if not applicable:
        return None
    exact = total * 10000 / applicable
    whole, remainder = divmod(exact.numerator, exact.denominator)
    return (whole + (2 * remainder >= exact.denominator)) / 100


def validate_submission(payload, expected_ids=None, dataset=None, conversations=None,
                        require_complete=False):
    """Validate exact appendix fields, values, arithmetic, IDs and source indices.

    Without source conversations this validates structure/arithmetic only; it
    cannot prove that a quoted turn or model rationale is semantically correct.
    """
    if not isinstance(payload, dict) or set(payload) != {'schema_version', 'team_name', 'model', 'results'}:
        raise ValueError('Submission must contain exactly schema_version, team_name, model, results')
    if payload['schema_version'] != '1.0':
        raise ValueError('schema_version must be 1.0')
    team = _text(payload['team_name'], 'team_name', 200)
    if team.casefold() in {'название команды', 'your team', 'team name'}:
        raise ValueError('Set the actual team name before exporting')
    model = payload['model']
    if not isinstance(model, dict) or set(model) != {'mode', 'name'}:
        raise ValueError('model must contain exactly mode and name')
    if not isinstance(model['mode'], str) or model['mode'] not in {'local', 'external_api', 'hybrid'}:
        raise ValueError('Unsupported model.mode')
    _model_name(model['name'])
    rows = payload['results']
    if not isinstance(rows, list):
        raise ValueError('results must be an array')
    if dataset is not None and (dataset not in COUNTS or len(rows) != COUNTS[dataset]):
        raise ValueError('Dataset must contain exactly 65 DEV or 10 PROD records')
    source = ({c['conversation_id']: c for c in conversations}
              if isinstance(conversations, list) else (conversations or {}))
    ids = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Each result must be an object')
        cid = _text(row.get('conversation_id'), 'conversation_id', 500)
        if cid in ids:
            raise ValueError('Duplicate conversation_id')
        ids.add(cid)
        status = row.get('status')
        if status == 'error':
            if set(row) != {'conversation_id', 'status', 'criteria', 'score', 'error'}:
                raise ValueError('Error row has unexpected or missing fields')
            if row['criteria'] != [] or row['score'] is not None:
                raise ValueError('Error rows require empty criteria and null score')
            _text(row['error'], 'error')
            if require_complete:
                raise ValueError('Complete export cannot contain error rows')
            continue
        if status != 'ok' or set(row) != {'conversation_id', 'status', 'criteria', 'score'}:
            raise ValueError('Successful row has unexpected or missing fields')
        criteria = row['criteria']
        if not isinstance(criteria, list) or len(criteria) != 8:
            raise ValueError('Successful results require all eight criteria')
        codes = set()
        for item in criteria:
            if not isinstance(item, dict) or set(item) != {'criterion_id', 'value', 'rationale', 'evidence_turns'}:
                raise ValueError('Criterion has unexpected or missing fields')
            code, value = item['criterion_id'], item['value']
            if not isinstance(code, str) or code not in WEIGHTS or code in codes:
                raise ValueError('Criterion IDs must be unique official codes')
            codes.add(code)
            if value is None:
                if code not in NA_CODES:
                    raise ValueError('N/A is not allowed for this criterion')
            elif code in NUMERIC_STEPS:
                if type(value) not in (int, float) or not math.isfinite(value) or value not in NUMERIC_STEPS[code]:
                    raise ValueError('Numeric criterion must use an official discrete step')
            elif type(value) is not bool:
                raise ValueError('Boolean criterion requires true/false')
            _text(item['rationale'], 'rationale')
            turns = item['evidence_turns']
            if (not isinstance(turns, list) or (not turns and value is not None)
                    or any(type(t) is not int or t < 1 for t in turns)):
                raise ValueError('evidence_turns must contain positive 1-based source indices')
            if turns != sorted(set(turns)):
                raise ValueError('evidence_turns must be unique and ascending')
            if cid in source and any(t > len(source[cid]['transcript']) for t in turns):
                raise ValueError('Evidence turn is outside the original transcript')
        result_score = row['score']
        if not isinstance(result_score, dict) or set(result_score) != {'final_score', 'critical_flag'}:
            raise ValueError('score requires exactly final_score and critical_flag')
        final_score = result_score['final_score']
        if type(final_score) not in (int, float) or not math.isfinite(final_score) or not 0 <= final_score <= 100:
            raise ValueError('final_score must be a finite number from 0 to 100')
        if final_score != _expected_score(criteria):
            raise ValueError('final_score differs from official weighted two-decimal result')
        flag = next(item['value'] for item in criteria if item['criterion_id'] == 'critical')
        if type(result_score['critical_flag']) is not bool or result_score['critical_flag'] is not flag:
            raise ValueError('critical_flag must match the critical answer')
    if expected_ids is not None and ids != set(expected_ids):
        raise ValueError('Exported conversation IDs differ from the source dataset')
    if source and not ids.issubset(source):
        raise ValueError('Export contains an unknown source conversation ID')
    return payload


def _official(checklist):
    try:
        candidate, canonical = validate_checklist(checklist), validate_checklist(official_checklist())
        return (candidate.get('provenance') == 'official'
                and candidate['criteria'] == canonical['criteria']
                and candidate['rules'] == canonical['rules'])
    except (ValueError, TypeError, KeyError):
        return False


def _row(conversation, evaluation, checklist, reviewed):
    snapshot = evaluation['final' if reviewed else 'original']
    answers = snapshot['answers']
    computed = score(checklist, answers, conversation)
    validate_evidence_roles(checklist, answers, conversation)
    positions = {m['id']: index + 1 for index, m in enumerate(conversation['transcript'])}
    criteria = []
    by_id = {a['criterion_id']: a for a in answers}
    for ident, code in CRITERION_CODES.items():
        answer = by_id[ident]
        rationale = _text(answer['explanation'], 'explanation')
        turns = {positions[ident] for ident in answer['evidence_message_ids']}
        span = answer.get('checked_span')
        if span:
            start, end = positions[span['start_message_id']], positions[span['end_message_id']]
            turns.update(range(start, end + 1))
            rationale += f' Проверенный участок: реплики {start}–{end}.'
        if answer['answer'] is None:
            rationale += ' N/A: ' + _text(answer.get('na_reason'), 'na_reason')
        if answer.get('missed') and not span:
            raise ValueError('Missing action has no checked source span')
        criteria.append({'criterion_id': code, 'value': answer['answer'],
                         'rationale': _public_rationale(rationale), 'evidence_turns': sorted(turns)})
    return {'conversation_id': conversation['conversation_id'], 'status': 'ok', 'criteria': criteria,
            'score': {'final_score': computed['total'], 'critical_flag': computed['gross_error']}}


# Accepted external grading runs: (engine, prompt revision).
EXTERNAL_RUNS = {('codex-cli', 'official-grounded-batch-v1'), ('claude-code', 'claude-direct-grading-v1')}


def export_results(store, dataset, group_id, team_name, reviewed=False, require_complete=False,
                   model_name=None, errors=None, provider_filter=None, include_external=False):
    """Export all source IDs, using the latest real official evaluation per ID.

    Original model answers are exported by default. ``reviewed=True`` explicitly
    selects human-reviewed final answers. Missing/invalid results are error rows,
    never zero scores or synthetic provider output. Errors never echo exceptions.
    """
    if dataset not in COUNTS:
        raise ValueError('dataset must be dev or prod')
    _text(group_id, 'group_id', 200)
    _text(team_name, 'team_name', 200)
    if type(include_external) is not bool:
        raise ValueError('include_external must be boolean')
    if provider_filter is not None:
        if not isinstance(provider_filter, dict) or not set(provider_filter).issubset({'model', 'prompt_revision', 'think'}):
            raise ValueError('provider_filter supports only model, prompt_revision and think')
        for key, value in provider_filter.items():
            if key == 'think':
                if type(value) is not bool:
                    raise ValueError('provider_filter.think must be boolean')
            else:
                _text(value, 'provider_filter.' + key, 500)
    conversations = [c for c in store.list('conversations') if c.get('dataset_group_id') == group_id]
    if len(conversations) != COUNTS[dataset]:
        raise ValueError('Selected source group must contain exactly 65 DEV or 10 PROD conversations')
    chats = sum(c.get('channel') == 'agent chat' for c in conversations)
    calls = sum(c.get('channel') == 'voice call' for c in conversations)
    if (dataset == 'dev' and (chats, calls) != (60, 5)) or (dataset == 'prod' and chats != 10):
        raise ValueError('DEV requires 60 chats and 5 calls; PROD requires 10 chats')
    checklists = {c['id']: c for c in store.list('checklists') if _official(c)}
    latest = {}
    for index, evaluation in enumerate(store.list('evaluations')):
        provider = evaluation.get('provider') or {}
        if evaluation.get('checklist_id') not in checklists:
            continue
        local = provider.get('type') == 'ollama'
        external = (include_external and provider.get('type') == 'external_api'
                    and (provider.get('engine'), provider.get('prompt_revision')) in EXTERNAL_RUNS
                    and provider.get('verified_run') is True
                    and isinstance(provider.get('model'), str) and bool(provider['model'].strip()))
        if not local and not external:
            continue
        if local and provider_filter and any(provider.get(k) != v for k, v in provider_filter.items()):
            continue
        cid = evaluation.get('conversation_id')
        key = (str(evaluation.get('created_at', '')), index)
        if cid not in latest or key > latest[cid][0]:
            latest[cid] = (key, evaluation)
    rows, names, modes = [], set(), set()
    for conversation in sorted(conversations, key=lambda c: c['conversation_id']):
        cid = conversation['conversation_id']
        entry = latest.get(cid)
        if entry:
            try:
                names.add(_model_name(entry[1]['provider']['model']))
                modes.add('local' if entry[1]['provider']['type'] == 'ollama' else 'external_api')
            except (ValueError, KeyError, TypeError):
                entry = None
        try:
            if not entry or (errors and cid in errors):
                raise ValueError('No eligible successful evaluation')
            evaluation = entry[1]
            row = _row(conversation, evaluation, checklists[evaluation['checklist_id']], reviewed)
            # Validate each row independently before it can become an "ok" row.
            probe = {'schema_version': '1.0', 'team_name': team_name,
                     'model': {'mode': 'local' if entry[1]['provider']['type'] == 'ollama' else 'external_api',
                               'name': entry[1]['provider']['model']}, 'results': [row]}
            validate_submission(probe, expected_ids=[cid], conversations=[conversation])
        except (ValueError, TypeError, KeyError, IndexError):
            row = {'conversation_id': cid, 'status': 'error', 'criteria': [], 'score': None,
                   'error': 'Не удалось получить действительную оценку официальным чек-листом.'}
        rows.append(row)
    if not names:
        names.add(_model_name(model_name or (provider_filter or {}).get('model') or os.environ.get('CQA_MODEL', '')))
        modes.add('local')
    mode = 'hybrid' if len(modes) > 1 else next(iter(modes))
    payload = {'schema_version': '1.0', 'team_name': team_name,
               'model': {'mode': mode, 'name': ', '.join(sorted(names))}, 'results': rows}
    return validate_submission(payload, expected_ids=[c['conversation_id'] for c in conversations],
                               dataset=dataset, conversations=conversations,
                               require_complete=require_complete)
