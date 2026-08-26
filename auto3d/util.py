"""Small shared helpers: paths, logging, subprocess, hashing, slugs, time."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# roots
#
# PROJECT_ROOT  this repository: orchestrator code, work/, config, job directories.
#               It is also the working directory Codex runs in, so every path the
#               prompts hand to the model is relative to it.
# SKILL_ROOT    the img2threejs skill checkout the pipeline drives. Vendored at
#               vendor/img2threejs; set IMG2THREEJS_ROOT to drive a different
#               checkout (e.g. an upstream clone you are tracking).
#
# The repository root carries symlinks (forge, grimoire, docs, skills, scripts,
# SKILL.md) into the vendored skill, so skill-relative paths written as
# `forge/...` or `grimoire/...` resolve from PROJECT_ROOT unchanged. Every forge
# script resolves its own root from __file__, so running one through the symlink
# still reads the vendored docs/ and grimoire/ next to it.
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT  # alias: the Codex working root
INTEGRATION_ROOT = PROJECT_ROOT  # alias: where auto3d.config.json lives

VENDORED_SKILL = PROJECT_ROOT / "vendor" / "img2threejs"


def resolve_skill_root() -> Path:
    """First candidate that looks like an img2threejs checkout wins."""
    override = os.environ.get("IMG2THREEJS_ROOT", "").strip()
    candidates = []
    if override:
        candidates.append(Path(override).expanduser())
    candidates += [
        VENDORED_SKILL,
        PROJECT_ROOT.parent / "img2threejs",
        Path.home() / ".codex" / "skills" / "img2threejs",
        Path.home() / ".claude" / "skills" / "img2threejs",
        Path.home() / ".agents" / "skills" / "img2threejs",
    ]
    for candidate in candidates:
        try:
            if (candidate / "SKILL.md").is_file() and (candidate / "forge").is_dir():
                return candidate.resolve()
        except OSError:
            continue
    return VENDORED_SKILL  # doctor reports the miss with a usable path


SKILL_ROOT = resolve_skill_root()
FORGE = SKILL_ROOT / "forge"


class Auto3DError(RuntimeError):
    """Raised for user-facing failures; the CLI prints the message without a traceback."""


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------

_VERBOSE = False
_LOG_FILE: Path | None = None


def set_verbose(value: bool) -> None:
    global _VERBOSE
    _VERBOSE = bool(value)


def set_log_file(path: Path | None) -> None:
    global _LOG_FILE
    _LOG_FILE = path
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def local_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def log(message: str, *, level: str = "info") -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    prefix = {"info": "•", "warn": "!", "error": "✗", "ok": "✓", "debug": "·"}.get(level, "•")
    line = f"[{stamp}] {prefix} {message}"
    if level != "debug" or _VERBOSE:
        stream = sys.stderr if level in {"warn", "error"} else sys.stdout
        print(line, file=stream, flush=True)
    if _LOG_FILE is not None:
        try:
            with _LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass


def debug(message: str) -> None:
    log(message, level="debug")


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, value: Any, *, indent: int = 2) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=indent, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_text(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_mtime(path: Path) -> float:
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def relpath(path: Path, start: Path = REPO_ROOT) -> str:
    """Repo-relative POSIX path when possible, else the absolute path."""
    try:
        return Path(path).resolve().relative_to(Path(start).resolve()).as_posix()
    except ValueError:
        return str(Path(path).resolve())


def which(binary: str) -> str | None:
    return shutil.which(binary)


# ---------------------------------------------------------------------------
# slugs and names
# ---------------------------------------------------------------------------


def slugify(value: str, *, max_length: int = 40, fallback: str = "subject") -> str:
    """ASCII slug for directory names. Korean/other scripts are transliterated where possible
    and otherwise dropped; the caller usually has an English ``subject_slug`` from the prompt
    author anyway."""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    text = re.sub(r"-{2,}", "-", text)
    if not text:
        return fallback
    return text[:max_length].rstrip("-") or fallback


def pascal_case(value: str) -> str:
    """Mirror of forge/stage3_build/generate_threejs_factory.py::pascal_case so the orchestrator
    can predict the factory function name from the target name."""
    words = re.findall(r"[A-Za-z0-9]+", value)
    return "".join(word[:1].upper() + word[1:] for word in words) or "Procedural"


# ---------------------------------------------------------------------------
# subprocess
# ---------------------------------------------------------------------------


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    check: bool = False,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    debug("$ " + " ".join(str(part) for part in command))
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    try:
        completed = subprocess.run(
            [str(part) for part in command],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=merged_env,
            input=input_text,
        )
    except subprocess.TimeoutExpired as exc:
        raise Auto3DError(f"command timed out after {timeout}s: {' '.join(command[:3])} …") from exc
    except FileNotFoundError as exc:
        raise Auto3DError(f"command not found: {command[0]}") from exc
    if check and completed.returncode != 0:
        raise Auto3DError(
            f"command failed ({completed.returncode}): {' '.join(command[:4])} …\n{completed.stderr.strip()[-2000:]}"
        )
    return completed


def run_forge(script: str, *args: object, cwd: Path = REPO_ROOT, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    """Run a forge script from the skill root (the scripts resolve paths relative to it)."""
    return run([sys.executable, str(FORGE / script), *[str(a) for a in args]], cwd=cwd, timeout=timeout)


def parse_json_output(text: str) -> Any:
    """Parse JSON from a script's stdout that may carry a banner line before the payload."""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start == -1:
        start = text.find("[")
    if start == -1:
        return None
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        return extract_first_json(text)


def extract_first_json(text: str) -> Any:
    """Return the first balanced JSON object/array found in free text (code fences allowed)."""
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        while start != -1:
            depth = 0
            in_string = False
            escape = False
            for index in range(start, len(text)):
                char = text[index]
                if in_string:
                    if escape:
                        escape = False
                    elif char == "\\":
                        escape = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
                    if depth == 0:
                        candidate = text[start : index + 1]
                        try:
                            return json.loads(candidate)
                        except json.JSONDecodeError:
                            break
            start = text.find(opener, start + 1)
    return None


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


class Stopwatch:
    def __init__(self) -> None:
        self.start = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.start


def chunked(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
