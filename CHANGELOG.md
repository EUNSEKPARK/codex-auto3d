# Changelog

All notable changes to codex-auto3d. Format loosely follows Keep a Changelog; versions are the
`__version__` in `auto3d/__init__.py`.

## [0.1.0] — 2026-08-25

First release as a standalone repository.

### Added

- `auto3d.py` CLI: `setup`, `doctor`, `prompt`, `run`, `resume`, `batch`, `preview`, `report`,
  `gallery`, `list`.
- Unattended pipeline: Codex authors a 3D-reconstruction-friendly image prompt → Codex's built-in
  `$imagegen` (gpt-image-2, no API key) generates the reference, gated by forge's
  `check_reference_admission.py` with up to three re-generations → Codex follows `SKILL.md` to
  author the spec and factory while Python bundles with esbuild, renders headless Chromium, captures
  12 views, builds the comparison sheet and runs the turntable / self-intersection / Tier-1 /
  interior-difference gates → review turns until the target pass passes → `report.html` and gallery.
- Batch input from `.csv`, `.xlsx`, `.txt` and `.json`, with English or Korean column names.
- `viewer/` interactive preview (orbit, view keys, turntable, wireframe) and world-space mesh export
  used by the self-intersection gate.
- Test suite driven by `tests/fake_codex.py`, a stand-in for the `codex` binary that runs the real
  forge scripts, so orchestration, rendering, gates and reports are exercised without an OpenAI login.
- `tools/sync_upstream.py`: vendoring tool for `vendor/img2threejs/` — `check` reports drift against
  `VENDORED.json`, `update --from <checkout>` re-vendors and records the upstream commit.

### Changed

- Extracted from `img2threejs/integrations/codex_auto3d/` into its own repository so the tool has its
  own version history instead of living on a branch of the upstream project.
- Split the roots that were previously one: `PROJECT_ROOT` (this repository, and Codex's working
  directory) and `SKILL_ROOT` (the img2threejs checkout, vendored at `vendor/img2threejs`, override
  with `IMG2THREEJS_ROOT`). Root symlinks keep every skill-relative path working unchanged.

### Vendored

- img2threejs @ `d37b6de` (upstream `main`, v1.5.1 line) — see `vendor/img2threejs/VENDORED.json`.
