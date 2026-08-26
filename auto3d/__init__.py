"""codex_auto3d — concept text → Codex-generated reference image → img2threejs procedural model.

The orchestrator is pure Python 3.10+ standard library, in keeping with the forge core. Browser
capture (Playwright + Chromium) and the TypeScript bundler (esbuild + three) are isolated optional
dependencies installed by ``auto3d.py setup`` into this repository's own ``.venv`` and ``node/``
directories; nothing here adds a runtime dependency to ``forge/``.

The img2threejs skill itself is vendored at ``vendor/img2threejs`` and reached through
``auto3d.util.SKILL_ROOT`` (override with ``IMG2THREEJS_ROOT``).
"""

from __future__ import annotations

__version__ = "0.4.0"
