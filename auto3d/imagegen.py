"""Reference image generation: Codex built-in `image_gen` (default) or the OpenAI Images API.

Both backends end with the same contract: a PNG at the requested path plus a record describing
how it was produced. The forge admission gate (`check_reference_admission.py`) decides whether the
image is usable as a 3D reference; on rejection the caller retries with feedback folded into the
prompt.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import shutil
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import prompts, schemas
from .codex import generated_images_dir, run_codex
from .config import Settings
from .util import Auto3DError, REPO_ROOT, log, parse_json_output, relpath, run_forge, write_json

IMAGES_API = "https://api.openai.com/v1/images"
MIN_IMAGE_BYTES = 2048  # anything smaller is not a real generated image


def _usable_image(path: Path) -> bool:
    return path.is_file() and path.stat().st_size >= MIN_IMAGE_BYTES and (is_png(path) or is_jpeg(path))


@dataclass
class ImageRecord:
    path: Path
    backend: str
    model: str | None
    size: str | None
    prompt_used: str
    thread_id: str | None = None
    notes: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": relpath(self.path),
            "backend": self.backend,
            "model": self.model,
            "size": self.size,
            "promptUsed": self.prompt_used,
            "threadId": self.thread_id,
            "notes": self.notes,
            "usage": self.usage,
        }


# ---------------------------------------------------------------------------
# admission
# ---------------------------------------------------------------------------


def probe_image(path: Path) -> dict[str, Any]:
    completed = run_forge("stage1_intake/probe_image.py", path)
    payload = parse_json_output(completed.stdout) or {}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("returncode", completed.returncode)
    return payload


def check_admission(path: Path, *, viewpoint: str = "reference", against: list[int] | None = None) -> dict[str, Any]:
    args: list[Any] = [path, "--viewpoint", viewpoint, "--json"]
    if against:
        args += ["--against", ",".join(str(value) for value in against)]
    completed = run_forge("stage1_intake/check_reference_admission.py", *args)
    payload = parse_json_output(completed.stdout)
    if not isinstance(payload, dict):
        return {"admitted": False, "reasons": [f"admission script failed: {completed.stderr.strip()[-400:]}"], "provenance": {}}
    return payload


def is_png(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(8) == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False


def is_jpeg(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(2) == b"\xff\xd8"
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Codex built-in backend
# ---------------------------------------------------------------------------


def _newest_generated_image(since: float) -> Path | None:
    root = generated_images_dir()
    if not root.is_dir():
        return None
    newest: tuple[float, Path] | None = None
    for candidate in root.rglob("*"):
        if not candidate.is_file() or candidate.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        mtime = candidate.stat().st_mtime
        if mtime >= since - 2 and (newest is None or mtime > newest[0]):
            newest = (mtime, candidate)
    return newest[1] if newest else None


def generate_with_codex(
    settings: Settings,
    prompt_text: str,
    out_path: Path,
    *,
    codex_dir: Path,
    label: str,
    retry_note: str | None = None,
    resume_thread: str | None = None,
    view: str | None = None,
) -> ImageRecord:
    out_rel = relpath(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if view:
        turn_prompt = prompts.view_turn(view, prompt_text, out_rel=out_rel, size=settings.image_size, quality=settings.image_quality)
    else:
        turn_prompt = prompts.imagegen_turn(prompt_text, out_rel=out_rel, size=settings.image_size, quality=settings.image_quality, retry_note=retry_note)
    schema_path = schemas.write_schema(schemas.IMAGEGEN_SCHEMA, codex_dir / "schemas" / "imagegen.schema.json")
    started = time.time()
    result = run_codex(
        settings,
        turn_prompt,
        label=label,
        events_path=codex_dir / f"{label}.events.jsonl",
        last_message_path=codex_dir / f"{label}.last.json",
        prompt_path=codex_dir / f"{label}.prompt.md",
        output_schema=schema_path,
        resume_thread=resume_thread,
        sandbox="workspace-write",
        network=False,
        cwd=REPO_ROOT,
        timeout_s=max(300, settings.turn_timeout_min * 60 // 2),
    )
    structured = result.structured if isinstance(result.structured, dict) else {}
    notes: list[str] = []
    if structured.get("notes"):
        notes.append(str(structured["notes"]))
    if result.errors:
        notes.extend(result.errors)

    if not _usable_image(out_path):
        # Codex sometimes reports a path but forgets the copy, or saves next to the default
        # location only. Recover the newest file from $CODEX_HOME/generated_images.
        recovered = _newest_generated_image(started)
        reported = structured.get("saved_path")
        candidates = [Path(str(reported)) if reported else None, (REPO_ROOT / str(reported)) if reported else None, recovered]
        for candidate in candidates:
            if candidate and _usable_image(candidate) and candidate.resolve() != out_path.resolve():
                shutil.copyfile(candidate, out_path)
                notes.append(f"recovered image from {candidate}")
                break
    if not _usable_image(out_path):
        raise Auto3DError(
            f"Codex did not produce a usable PNG/JPEG at {out_rel}. Check {codex_dir / (label + '.events.jsonl')} — "
            "is `codex login` valid and is the imagegen skill enabled?"
        )
    return ImageRecord(
        path=out_path,
        backend="codex",
        model=str(structured.get("model") or settings.image_model),
        size=str(structured.get("size") or settings.image_size),
        prompt_used=str(structured.get("prompt_used") or prompt_text),
        thread_id=result.thread_id,
        notes=notes,
        usage=result.usage,
    )


# ---------------------------------------------------------------------------
# OpenAI Images API backend (stdlib only)
# ---------------------------------------------------------------------------


def _api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise Auto3DError("OPENAI_API_KEY is not set; the API image backend needs it (or use --image-backend codex)")
    return key


def _post_json(url: str, payload: dict[str, Any], *, timeout: float = 300) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
        method="POST",
    )
    return _send(request, timeout=timeout)


def _post_multipart(url: str, fields: dict[str, str], files: list[tuple[str, Path]], *, timeout: float = 300) -> dict[str, Any]:
    boundary = f"----auto3d{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8")
    for name, path in files:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body += (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{path.name}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        body += path.read_bytes()
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    request = urllib.request.Request(
        url,
        data=bytes(body),
        headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    return _send(request, timeout=timeout)


def _send(request: urllib.request.Request, *, timeout: float) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1500]
        raise Auto3DError(f"Images API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise Auto3DError(f"Images API unreachable: {exc.reason}") from exc


def generate_with_api(
    settings: Settings,
    prompt_text: str,
    out_path: Path,
    *,
    reference_images: list[Path] | None = None,
) -> ImageRecord:
    model = settings.image_model
    fields: dict[str, Any] = {"model": model, "prompt": prompt_text, "n": 1, "size": settings.image_size, "quality": settings.image_quality}
    if reference_images:
        payload = _post_multipart(
            f"{IMAGES_API}/edits",
            {key: str(value) for key, value in fields.items()},
            [("image[]", path) for path in reference_images],
        )
    else:
        fields["output_format"] = "png"
        payload = _post_json(f"{IMAGES_API}/generations", fields)
    data = payload.get("data") or []
    if not data or not data[0].get("b64_json"):
        raise Auto3DError(f"Images API returned no image: {json.dumps(payload)[:500]}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base64.b64decode(data[0]["b64_json"]))
    usage = payload.get("usage") or {}
    return ImageRecord(
        path=out_path,
        backend="api",
        model=model,
        size=settings.image_size,
        prompt_used=prompt_text,
        usage={key: int(value) for key, value in usage.items() if isinstance(value, int)},
    )


# ---------------------------------------------------------------------------
# orchestration helper
# ---------------------------------------------------------------------------


def generate_reference(
    settings: Settings,
    prompt_text: str,
    out_path: Path,
    *,
    codex_dir: Path,
    label: str,
    retry_note: str | None = None,
    resume_thread: str | None = None,
    view: str | None = None,
    reference_images: list[Path] | None = None,
) -> ImageRecord:
    backend = settings.image_backend
    if backend in {"codex", "auto"}:
        try:
            return generate_with_codex(
                settings,
                prompt_text,
                out_path,
                codex_dir=codex_dir,
                label=label,
                retry_note=retry_note,
                resume_thread=resume_thread,
                view=view,
            )
        except Auto3DError as exc:
            if backend == "codex" or not os.environ.get("OPENAI_API_KEY"):
                raise
            log(f"codex image backend failed ({exc}); falling back to the Images API", level="warn")
    record = generate_with_api(settings, prompt_text, out_path, reference_images=reference_images)
    return record


def admission_feedback(verdict: dict[str, Any]) -> str:
    reasons = verdict.get("reasons") or []
    hints = []
    for reason in reasons:
        text = str(reason)
        if "coverage" in text and ">" in text:
            hints.append("the subject fills the whole frame — add clear empty light-grey margin (12-15%) on every side so the silhouette can be isolated")
        elif "coverage" in text and "<" in text:
            hints.append("the subject is too small — it must fill most of the frame (about 70-80% of the width)")
        elif "coherence" in text:
            hints.append("the subject reads as several scattered fragments — show ONE connected object with a solid silhouette against a plain backdrop")
        elif "resolution" in text:
            hints.append("the image is too small — use a larger output size")
        elif "duplicate" in text:
            hints.append("this view duplicates an existing reference — change the camera angle clearly")
        else:
            hints.append(text)
    return "; ".join(hints) or "the previous image was rejected by the reference-admission gate"


def save_record(record: ImageRecord, path: Path) -> None:
    write_json(path, record.as_dict())
