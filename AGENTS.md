# Repository notes for Codex

This repository orchestrates the **img2threejs** skill. Your working directory is the repository
root; every path in the task prompt is relative to it.

- The skill lives at `vendor/img2threejs/`. The root entries `SKILL.md`, `forge/`, `grimoire/`,
  `docs/`, `skills/` and `scripts/` are symlinks into it, so `python3 forge/state.py …` and
  `Read grimoire/…` work from the root exactly as SKILL.md describes them.
- `vendor/` is read-only. Never edit, move or delete anything under it, and never edit `auto3d/`,
  `tools/` or `tests/` during a job — only the job directory you are given may change.
- Job directories live under `work/auto3d/<stamp>-<slug>/`. Write specs, factories, state and
  evidence there.
- The orchestrator checks your work against disk (spec `reviewHistory`, factory hash, gate JSON),
  so report only what you actually ran.
- No network access is available during a job, and nothing needs installing — the toolchain is
  already in `node/` and `.venv/`.
