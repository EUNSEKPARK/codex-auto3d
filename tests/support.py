from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
INTEGRATION_ROOT = PROJECT_ROOT  # importable path for the auto3d package
REPO_ROOT = PROJECT_ROOT  # the Codex working root the fake CLI runs in
FAKE_CODEX = TESTS_DIR / "fake_codex.py"

if str(INTEGRATION_ROOT) not in sys.path:
    sys.path.insert(0, str(INTEGRATION_ROOT))


def make_codex_shim(directory: Path, *, mode: str = "") -> Path:
    """Create an executable `codex` that forwards to fake_codex.py."""
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "codex"
    shim.write_text(
        "#!/bin/sh\n"
        f"FAKE_CODEX_MODE='{mode}' FAKE_CODEX_REPO='{REPO_ROOT}' exec '{sys.executable}' '{FAKE_CODEX}' \"$@\"\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def temp_dir(prefix: str = "auto3d-test-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def browser_available() -> bool:
    from auto3d.preview import esbuild_bin, playwright_python, three_installed

    return esbuild_bin() is not None and three_installed() and playwright_python() is not None and os.environ.get("AUTO3D_SKIP_BROWSER_TESTS") != "1"
