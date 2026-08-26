"""Typecast API client — standard library only, in keeping with the rest of this repo.

Two endpoints matter here:

    POST /v1/text-to-speech/with-timestamps   audio + per-word and per-character timings
    GET  /v2/voices                           the voice catalogue

The plain `/v1/text-to-speech` returns audio and nothing else, which would leave us doing forced
alignment ourselves; the with-timestamps variant is what makes lip-sync tractable, so it is the
one this module uses.

Authentication is an `X-API-KEY` header, read from TYPECAST_API_KEY unless passed explicitly. The
key is never written into a job directory or a log line.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

API_BASE = "https://api.typecast.ai"
DEFAULT_MODEL = "ssfm-v30"
DEFAULT_TIMEOUT = 180.0

# the console shows bare 24-hex ids while the API wants them prefixed
BARE_ID = re.compile(r"^[0-9a-f]{24}$")


class TypecastError(RuntimeError):
    """Raised for anything the caller can act on: a missing key, a rejected request, a bad body."""


def api_key(explicit: str | None = None) -> str:
    key = (explicit or os.environ.get("TYPECAST_API_KEY") or "").strip()
    if not key:
        raise TypecastError("no API key — set TYPECAST_API_KEY (Typecast console → API keys) or pass --api-key")
    return key


def normalize_voice_id(voice_id: str) -> str:
    """The character list in the Typecast console shows ids like `65bb3a1976b69213594357fc`; the
    v1 API expects `tc_65bb3a1976b69213594357fc`. Accept either."""
    voice_id = (voice_id or "").strip()
    return f"tc_{voice_id}" if BARE_ID.match(voice_id) else voice_id


@dataclass
class Speech:
    audio: bytes
    audio_format: str
    duration: float
    words: list[dict[str, Any]] = field(default_factory=list)
    characters: list[dict[str, Any]] = field(default_factory=list)
    voice_id: str = ""
    model: str = ""

    def write_audio(self, directory: Path, stem: str = "speech") -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{stem}.{self.audio_format or 'wav'}"
        path.write_bytes(self.audio)
        return path

    def as_dict(self) -> dict[str, Any]:
        return {
            "voiceId": self.voice_id,
            "model": self.model,
            "audioFormat": self.audio_format,
            "durationSec": round(self.duration, 4),
            "words": self.words,
            "characters": self.characters,
        }


def _request(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None, key: str, timeout: float) -> Any:
    url = urllib.parse.urljoin(API_BASE, path)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"X-API-KEY": key, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:  # the body usually says exactly what is wrong
        detail = exc.read().decode("utf-8", "replace")[:600]
        raise TypecastError(f"{method} {path} failed: HTTP {exc.code} {exc.reason}\n{detail}") from exc
    except urllib.error.URLError as exc:
        raise TypecastError(f"{method} {path} failed: {exc.reason}") from exc
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TypecastError(f"{method} {path} returned {len(raw)} bytes that are not JSON") from exc


def list_voices(*, model: str | None = DEFAULT_MODEL, key: str | None = None, timeout: float = 60.0) -> list[dict[str, Any]]:
    query = f"?{urllib.parse.urlencode({'model': model})}" if model else ""
    payload = _request(f"/v2/voices{query}", key=api_key(key), timeout=timeout)
    if isinstance(payload, dict):
        payload = payload.get("voices") or payload.get("data") or []
    return list(payload or [])


def speak(
    text: str,
    voice_id: str,
    *,
    model: str = DEFAULT_MODEL,
    language: str | None = None,
    granularity: str = "char",
    output: dict[str, Any] | None = None,
    prompt: dict[str, Any] | None = None,
    seed: int | None = None,
    key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Speech:
    """Synthesise `text` and return the audio together with its timings.

    `granularity="char"` is the default on purpose: Korean has no spaces inside a word the way
    English does, so word-level alignment collapses to a handful of long segments, while a
    character *is* a syllable and gives one mouth shape each.
    """
    if not text.strip():
        raise TypecastError("nothing to say: text is empty")
    payload: dict[str, Any] = {"text": text, "voice_id": normalize_voice_id(voice_id), "model": model}
    if language:
        payload["language"] = language
    if output:
        payload["output"] = output
    if prompt:
        payload["prompt"] = prompt
    if seed is not None:
        payload["seed"] = seed
    path = "/v1/text-to-speech/with-timestamps"
    if granularity in {"word", "char"}:
        path += f"?{urllib.parse.urlencode({'granularity': granularity})}"

    data = _request(path, method="POST", payload=payload, key=api_key(key), timeout=timeout)
    if not isinstance(data, dict) or "audio" not in data:
        raise TypecastError(f"unexpected response shape: {str(data)[:300]}")
    try:
        audio = base64.b64decode(data["audio"])
    except (TypeError, ValueError) as exc:
        raise TypecastError("the `audio` field is not valid base64") from exc
    return Speech(
        audio=audio,
        audio_format=str(data.get("audio_format") or "wav"),
        duration=float(data.get("audio_duration") or 0.0),
        words=list(data.get("words") or []),
        characters=list(data.get("characters") or []),
        voice_id=payload["voice_id"],
        model=model,
    )
