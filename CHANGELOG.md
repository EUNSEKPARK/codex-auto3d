# Changelog

All notable changes to codex-auto3d. Format loosely follows Keep a Changelog; versions are the
`__version__` in `auto3d/__init__.py`.

## [0.3.0] — 2026-08-26

Fixes found by the first real character build (a supplied-reference mascot, blocked at blockout
with fidelity 0.62 after 1h45m).

### Fixed

- **Render framing is calibrated against the reference** instead of using a fixed camera margin.
  The old constant put a supplied 1024² reference's render 33% under scale, so the Tier-1 scale
  gate failed on the framing alone every turn and the review turns spent their correction budget
  on the pipeline rather than the model. `render_factory` now probes one hero capture, measures
  the silhouette bbox against the reference with the gate's own `load_mask`/`bbox_of`, and
  corrects the margin (bbox area goes as 1/margin²): measured 0.337 → 0.021 scale delta in two
  probes, ~20s once per job. The result is cached on the job as `framingMargin`, and reported in
  `capture.json` under `framing`.
- **Token accounting no longer multiplies.** Codex reports usage for the whole thread, so every
  resumed turn repeats what the earlier turns already reported; summing them made one build
  thread read as 113.8M input tokens when its fresh input was about 1M. Usage is now recorded as
  the growth per thread, and `list`/`report.html` separate new input from cached.

### Changed

- `max_corrections_per_pass` 3 → 5 and `max_corrections_total` 6 → 10. A character blockout has
  to converge silhouette and proportion before it can pass; 3 hard-stopped a run that was still
  improving. `max_review_turns` remains the real ceiling on cost.
- New `first_turn_timeout_min` (default 120): the first build turn does intake, assessment,
  detail inventory, spec authoring, strict validation and the blockout factory in one turn, and
  a character run exceeded the 60-minute per-turn budget mid-flight.

## [0.2.0] — 2026-08-26

### Added

- `run --reference IMAGE` builds from an image you already have instead of generating one:
  the prompt stage becomes an intake turn that *reads* the supplied images (subject, profile,
  complexity, identity features, and a camera estimate) and the image stage adopts the files
  instead of calling `$imagegen`. `--view NAME=PATH` (front|side|back|top, repeatable) supplies
  the other angles, and `--reference-camera AZ,EL` pins the hero camera when you know it.
  Every supplied file goes through the same admission and near-duplicate gates as a generated
  one; a hero that fails is reported and kept, since there is nothing to regenerate.
- `tools/prepare_reference.py`: turns delivered art into admissible references — splits a
  turnaround sheet into figures, flattens transparency onto the pipeline backdrop, and frames
  every figure on one canvas at a single scale with a common baseline. Standard library only
  (it reads and writes PNG/JPEG through the vendored forge decoders).
- Tests for the supplied-reference path, including a full build from one, and a fake-Codex
  intake handler.

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
