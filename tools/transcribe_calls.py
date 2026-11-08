"""Optional local recording transcription; never replace organizer transcripts.

Run with a project-local Python environment containing faster-whisper and PyAV:
    python tools/transcribe_calls.py --device cuda --compute-type int8_float16

Recordings, model files and the SQLite database remain inside this project.
Recognized segments have recording-relative times and deliberately have no
speaker roles: speech recognition is not verified speaker diarization.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.audio import resolve_audio
from server.storage import Store
from server.transcriptions import validate_transcription

MAX_AUDIO_BYTES = 100 * 1024 * 1024
DOWNLOAD_SECONDS = 60
_DLL_HANDLES = []


def project_path(value):
    path = Path(value)
    path = (ROOT / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError("path must remain inside the project")
    return path


def configure_windows_dlls():
    """Keep handles alive so deferred CTranslate2 CUDA loads can find DLLs."""
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return
    directories = [ROOT / ".local/ollama/lib/ollama/cuda_v12"]
    nvidia = ROOT / ".local/asr-venv/Lib/site-packages/nvidia"
    if nvidia.is_dir():
        directories.extend(nvidia.glob("*/bin"))
    for directory in directories:
        if directory.is_dir():
            _DLL_HANDLES.append(os.add_dll_directory(str(directory)))
            # CTranslate2 also loads CUDA libraries lazily with LoadLibraryA.
            # Its lookup uses PATH rather than Python's extension search flags.
            os.environ['PATH'] = str(directory) + os.pathsep + os.environ.get('PATH', '')


def configure_local_caches():
    """Constrain optional model caches and decode temporaries to project state."""
    import tempfile
    local = ROOT / ".local"
    defaults = {
        "HF_HOME": local / "hf-cache",
        "HF_HUB_CACHE": local / "hf-cache/hub",
        "XDG_CACHE_HOME": local / "cache",
        "TEMP": local / "tmp",
        "TMP": local / "tmp",
    }
    for name, default in defaults.items():
        try:
            supplied = project_path(os.environ[name])
            selected = supplied if supplied.is_relative_to(local) else default
        except (KeyError, ValueError):
            selected = default
        selected.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(selected)
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    # Some libraries import tempfile before their environment is configured.
    tempfile.tempdir = os.environ["TMP"]


def _https_url(url):
    p = urlsplit(url)
    return (p.scheme == "https" and bool(p.hostname)
            and p.username is None and p.password is None)


class _HttpsRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _https_url(newurl):
            raise ValueError("recording redirect must use HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_audio(conversation, cache_dir, opener=None):
    """Bounded private download; exceptions must not be printed verbatim."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(conversation["conversation_id"].encode()).hexdigest()
    destination = cache_dir / (key + ".mp3")
    if destination.is_file() and 0 < destination.stat().st_size <= MAX_AUDIO_BYTES:
        return destination
    resolved = resolve_audio(conversation.get("audio_url"))
    url = resolved.get("media_url")
    if resolved.get("status") != "playable" or not url or not _https_url(url):
        raise ValueError("recording is not available as HTTPS audio")
    opener = opener or build_opener(ProxyHandler({}), _HttpsRedirect())
    temporary = cache_dir / (key + "." + uuid.uuid4().hex + ".part")
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    try:
        request = Request(url, headers={"Accept": "audio/*, application/octet-stream"})
        with opener.open(request, timeout=DOWNLOAD_SECONDS) as response, temporary.open("wb") as output:
            length = response.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > MAX_AUDIO_BYTES:
                raise ValueError("recording exceeds the download limit")
            size = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("recording download timed out")
                # urllib's timeout otherwise applies separately to each read.
                # Set the remaining total budget on its underlying TLS socket.
                sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                if sock is not None:
                    sock.settimeout(remaining)
                chunk = response.read(min(64 * 1024, MAX_AUDIO_BYTES - size + 1))
                if time.monotonic() > deadline:
                    raise TimeoutError("recording download timed out")
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_AUDIO_BYTES:
                    raise ValueError("recording exceeds the download limit")
                output.write(chunk)
            if size == 0:
                raise ValueError("recording download is empty")
        temporary.replace(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def normalize_segments(segments, duration):
    """Validate model timing without inventing roles or rewriting source text."""
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("audio duration must be positive and finite")
    result = []
    previous_start = -1.0
    for segment in segments:
        start, end, text = segment.start, segment.end, segment.text
        if (isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                or not math.isfinite(start) or not math.isfinite(end)
                or start < 0 or end < start or start < previous_start
                or end > duration + 0.25 or start > duration):
            raise ValueError("ASR segment timing is outside the recording")
        if not isinstance(text, str):
            raise ValueError("ASR segment text must be a string")
        previous_start = start
        if not text.strip():
            continue
        result.append({"id": f"asr-{len(result)}", "start": float(start),
                       "end": min(float(end), float(duration)), "text": text.strip()})
    if not result:
        raise ValueError("recognition returned no speech segments")
    return result


def refine_segment_languages(model, segments, audio, duration, base_language, sample_rate=16000):
    """Re-decode confident language switches in their actual short audio spans.

    A single 30-second language decision loses a short Kazakh IVR phrase inside
    mostly Russian speech. Detect on each speech segment; never rewrite words
    using a dictionary, another transcript or an invented canonical greeting.
    """
    result = []
    for segment in segments:
        row = dict(segment, language=base_language)
        start, end = segment['start'], segment['end']
        clip = audio[int(start * sample_rate):int(end * sample_rate)]
        if end - start < 1 or not len(clip):
            result.append(row)
            continue
        detected, probability, _ = model.detect_language(audio=clip)
        if detected not in ('ru', 'kk', 'en') or probability < 0.8 or detected == base_language:
            result.append(row)
            continue
        candidates, _ = model.transcribe(
            clip, language=detected, beam_size=5, temperature=0.0,
            vad_filter=False, word_timestamps=False, condition_on_previous_text=False,
            multilingual=False,
        )
        candidates = normalize_segments(candidates, end - start)
        for candidate in candidates:
            candidate.update(start=min(end, start + candidate['start']),
                             end=min(end, start + candidate['end']),
                             language=detected)
            result.append(candidate)
    # Candidate IDs are local to their clipped audio: reassign globally.
    for index, segment in enumerate(result):
        segment['id'] = f'asr-{index}'
    return result


def transcribe_one(model, conversation, audio_path, model_name, device, compute_type):
    language = {"RU": "ru", "KZ": "kk", "EN": "en"}[conversation["language"]]
    started = time.monotonic()
    segments, info = model.transcribe(
        str(audio_path), language=language, beam_size=5, temperature=0.0,
        vad_filter=True, word_timestamps=False, condition_on_previous_text=False,
        multilingual=True,
    )
    # The iterator performs inference lazily. Consume and validate before saving
    # so interruptions/errors leave a previously saved result untouched.
    normalized = normalize_segments(segments, info.duration)
    if hasattr(model, 'detect_language'):
        from faster_whisper.audio import decode_audio
        audio = decode_audio(str(audio_path), sampling_rate=16000)
        normalized = refine_segment_languages(model, normalized, audio, info.duration, language)
    digest = hashlib.sha256()
    with audio_path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "conversation_id": conversation["conversation_id"], "status": "ready",
        "source": "recording", "engine": "faster-whisper", "model": model_name,
        "language": language, "original_language": conversation["language"],
        "detected_language": getattr(info, "language", language),
        "created_at": datetime.now(timezone.utc).isoformat(), "reviewed": False,
        "segments": normalized, "audio_duration_seconds": float(info.duration),
        "audio_sha256": digest.hexdigest(), "device": device, "compute_type": compute_type,
        "transcription_seconds": round(time.monotonic() - started, 2),
        "speaker_attribution": "unassigned",
        "language_detection": "per_speech_segment",
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=".local/cqa.sqlite3")
    parser.add_argument("--model", default="large-v3-turbo")
    parser.add_argument("--model-dir", default=".local/whisper-model")
    parser.add_argument("--cache-dir", default=".local/audio-cache")
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--compute-type", default=None, help="Default: int8_float16 on CUDA, int8 on CPU")
    parser.add_argument("--conversation", help="Process one conversation ID, e.g. to resume a batch")
    parser.add_argument("--force", action="store_true", help="Replace saved ASR result only; never alter organizer data")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        database, cache, model_dir = map(project_path, (args.database, args.cache_dir, args.model_dir))
        if not database.is_file():
            raise ValueError("application database does not exist")
        store = Store(database)
        voices = [c for c in store.list("conversations") if c.get("channel") == "voice call"
                  and (not args.conversation or c["conversation_id"] == args.conversation)]
        if not voices:
            raise ValueError("no matching voice conversations")
        pending = []
        for conversation in voices:
            try:
                validate_transcription(store.get('transcriptions', conversation['conversation_id']), conversation['conversation_id'])
                ready = True
            except ValueError:
                ready = False
            if args.force or not ready:
                pending.append(conversation)
        print(json.dumps({"event": "batch", "selected": len(voices), "pending": len(pending), "skipped": len(voices) - len(pending)}), flush=True)
        if not pending:
            return 0
        configure_local_caches()
        configure_windows_dlls()
        from faster_whisper import WhisperModel
        compute = args.compute_type or ("int8" if args.device == "cpu" else "int8_float16")
        model_dir.mkdir(parents=True, exist_ok=True)
        local_model = str(model_dir) if (model_dir / "model.bin").is_file() else args.model
        model = WhisperModel(local_model, device=args.device, compute_type=compute, download_root=str(model_dir))
        failures = 0
        for conversation in pending:
            safe_id = re.sub(r"[^A-Za-z0-9_.:-]", "_", conversation["conversation_id"])[:100]
            print(json.dumps({"event": "start", "conversation_id": safe_id}), flush=True)
            try:
                audio = download_audio(conversation, cache)
                result = transcribe_one(model, conversation, audio, args.model, args.device, compute)
                result = validate_transcription(result, conversation["conversation_id"])
                store.put("transcriptions", conversation["conversation_id"], result)
                print(json.dumps({"event": "ready", "conversation_id": safe_id,
                                  "segments": len(result["segments"]),
                                  "audio_seconds": round(result["audio_duration_seconds"], 2),
                                  "elapsed_seconds": result["transcription_seconds"]}), flush=True)
            except Exception as error:
                failures += 1
                # Exception strings can contain signed recording URLs or speech.
                print(json.dumps({"event": "failed", "conversation_id": safe_id, "error_type": type(error).__name__}), flush=True)
        print(json.dumps({"event": "finished", "processed": len(pending), "failures": failures}), flush=True)
        return int(bool(failures))
    except Exception as error:
        print(json.dumps({"event": "failed", "error_type": type(error).__name__}), flush=True)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('{"event":"interrupted"}', flush=True)
        raise SystemExit(130)
