"""Validated dataset ingestion. Source transcripts and signed URLs stay local."""
from datetime import datetime
import json
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


MAX_DATASET_BYTES = 20 * 1024 * 1024
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _offset(timestamp, first):
    try:
        if ":" in timestamp and "T" not in timestamp:
            minutes, seconds = timestamp.split(":")
            return int(minutes) * 60 + float(seconds)
        current = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        start = datetime.fromisoformat(first.replace("Z", "+00:00"))
        return max(0, (current - start).total_seconds())
    except (ValueError, TypeError):
        return None


def normalize_dataset(payload, group_id="JSON"):
    """Return validated records, preserving roles and original timestamp strings."""
    if isinstance(payload, dict):
        if "isSuccess" in payload:
            if payload["isSuccess"] is not True:
                raise ValueError("Dataset provider did not report success")
            body = payload.get("data")
            if not isinstance(body, dict):
                raise ValueError("Dataset data must be an object")
        else:
            body = payload
        group_id = body.get("dataset_group_id", group_id)
        records = body.get("records")
    else:
        records = payload
    if not isinstance(group_id, str) or not group_id.strip():
        raise ValueError("dataset_group_id must be a nonempty string")
    if not isinstance(records, list):
        raise ValueError("Dataset records must be an array")
    result, seen = [], set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Conversation must be an object")
        cid = record.get("conversation_id")
        if not isinstance(cid, str) or not cid.strip() or cid in seen:
            raise ValueError("Conversation IDs must be nonempty and unique")
        seen.add(cid)
        if record.get("language") not in ("RU", "KZ", "EN"):
            raise ValueError("Unsupported conversation language")
        if record.get("channel") not in ("agent chat", "voice call"):
            raise ValueError("Unsupported conversation channel")
        transcript = record.get("transcript")
        if not isinstance(transcript, list) or not transcript:
            raise ValueError("Transcript must be a nonempty array")
        audio = record.get("audio_url")
        if audio is not None and not isinstance(audio, str):
            raise ValueError("audio_url must be a string or null")
        if audio and urlsplit(audio).scheme not in ("http", "https"):
            raise ValueError("audio_url must use HTTP or HTTPS")
        messages = []
        for index, message in enumerate(transcript):
            if not isinstance(message, dict):
                raise ValueError("Message must be an object")
            if message.get("speaker") not in ("operator", "client", "bot", "system"):
                raise ValueError("Unsupported transcript role")
            if not isinstance(message.get("timestamp"), str) or not message["timestamp"]:
                raise ValueError("Message timestamp must be a nonempty string")
            if not isinstance(message.get("text"), str):
                raise ValueError("Message text must be a string")
            item = dict(message, id=f"{cid}:{index}")
            offset = _offset(message["timestamp"], transcript[0].get("timestamp", ""))
            if offset is not None:
                item["audio_offset_seconds"] = offset
            messages.append(item)
        result.append(dict(record, transcript=messages, dataset_group_id=group_id))
    return result


def fetch_dataset(group_id="DEV"):
    """Fetch configured organizer endpoint without exposing its signed query."""
    if not isinstance(group_id, str) or not group_id.strip():
        raise ValueError("group_id must be a nonempty string")
    url = os.environ.get("CQA_DEV_URL", "").strip()
    if not url:
        config = PROJECT_ROOT / ".local" / "dev-url.txt"
        if config.exists():
            url = config.read_text(encoding="utf-8").strip()
    if not url:
        raise ValueError("DEV endpoint is not configured")
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError()
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "group_id"]
        query.append(("group_id", group_id))
        target = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
        with urlopen(Request(target, headers={"Accept": "application/json"}), timeout=30) as response:
            raw = response.read(MAX_DATASET_BYTES + 1)
        if len(raw) > MAX_DATASET_BYTES:
            raise ValueError()
        payload = json.loads(raw)
    except Exception:
        raise ValueError("Dataset request failed; check local endpoint configuration and connectivity") from None
    return normalize_dataset(payload, group_id)
