"""Separate recording-derived text; original role-tagged evidence is immutable."""
from copy import deepcopy
from datetime import datetime
import math


def validate_transcription(value, conversation_id=None):
    if not isinstance(value, dict) or value.get('status') != 'ready' or value.get('source') != 'recording':
        raise ValueError('Invalid recording transcription')
    if conversation_id is not None and value.get('conversation_id') != conversation_id:
        raise ValueError('Recording transcription belongs to another conversation')
    for field in ('model', 'language', 'created_at'):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError('Recording transcription metadata required')
    try:
        created = datetime.fromisoformat(value['created_at'].replace('Z', '+00:00'))
        if created.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise ValueError('Recording transcription timestamp must include timezone') from None
    if value.get('reviewed') is not False:
        # This path is automatic recognition, never a human verification claim.
        raise ValueError('Automatic recording transcription is unreviewed')
    duration = value.get('audio_duration_seconds')
    if type(duration) not in (float, int) or not math.isfinite(duration) or not 0 < duration <= 86400:
        raise ValueError('Invalid recording duration')
    segments = value.get('segments')
    if not isinstance(segments, list) or not segments or len(segments) > 100000:
        raise ValueError('Recording transcription requires segments')
    last_start, seen = -1, set()
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError('Invalid recording segment')
        ident = segment.get('id')
        if not isinstance(ident, str) or not ident or ident in seen:
            raise ValueError('Recording segment IDs must be unique')
        seen.add(ident)
        start, end = segment.get('start'), segment.get('end')
        if any(type(v) not in (float, int) or not math.isfinite(v) for v in (start, end)):
            raise ValueError('Recording offsets must be finite numbers')
        if start < last_start or start < 0 or end < start or end > duration + 1:
            raise ValueError('Recording segment outside ordered audio bounds')
        last_start = start
        if not isinstance(segment.get('text'), str) or not segment['text'].strip():
            raise ValueError('Recording segment text required')
        if 'speaker' in segment:
            raise ValueError('Automatic transcription does not establish speaker roles')
    return deepcopy(value)


def attach_transcription(conversation, store):
    """Enrich a read view only; never mutate source or saved evaluation inputs."""
    result = deepcopy(conversation)
    result.pop('recording_transcription', None)
    if conversation.get('channel') != 'voice call':
        return result
    record = store.get('transcriptions', conversation['conversation_id'])
    if record is not None:
        try:
            result['recording_transcription'] = validate_transcription(record, conversation['conversation_id'])
        except ValueError:
            result['recording_transcription'] = {'status': 'invalid', 'message': 'Расшифровка записи недоступна. Исходные реплики сохранены.'}
    return result
