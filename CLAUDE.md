# Project instructions

`codex-auto3d` drives the Codex CLI through the whole img2threejs loop unattended: concept text →
image prompt → reference image → procedural Three.js model → render, gates, report. The
img2threejs skill it drives is **vendored** at `vendor/img2threejs/`, not depended on externally.

## Layout

- `auto3d.py`, `auto3d/` — the orchestrator. Python 3.10+, standard library only.
- `tests/` — the suite, driven by `tests/fake_codex.py` (a stand-in for the `codex` binary).
- `vendor/img2threejs/` — the vendored skill. `tools/sync_upstream.py` owns this directory.
- `forge`, `grimoire`, `docs`, `skills`, `scripts`, `SKILL.md` — symlinks into `vendor/img2threejs/`,
  so skill-relative paths resolve from the repository root. Do not replace them with real
  directories; the prompts and SKILL.md depend on those paths existing at the root.
- `auto3d.util.PROJECT_ROOT` is this repository (and Codex's working directory);
  `auto3d.util.SKILL_ROOT` is the skill checkout (`IMG2THREEJS_ROOT` overrides it). Keep the two
  distinct — conflating them is what tied this tool to someone else's repository in the first place.

## Change rules

- Preserve the code-only procedural Three.js contract; never download meshes or art packs.
- Keep claims honest: distinguish implemented capability from roadmap or design-only documentation,
  and say plainly what was verified with a real Codex login versus the fake CLI.
- Never hand-edit `vendor/img2threejs/`. Refresh it with `tools/sync_upstream.py update`; if a local
  patch is unavoidable, record it in `vendor/img2threejs/VENDORED.json` under `localPatches` so
  `check` and the next `update` both see it.
- After refreshing the vendored skill, re-check the forge script flags baked into
  `auto3d/prompts.py` and run the tests — a silently renamed flag turns into a burned Codex turn.
- Keep backward compatibility for existing job directories under `work/auto3d/`; `resume` must
  still open a job written by an earlier version.
- When changing schemas, gates, prompts or review behavior, add or update focused tests.
- Keep `README.md`, `README.ko.md` and `CHANGELOG.md` consistent when user-facing behavior changes.

## Verification

```bash
cd tests && python3 -m unittest -v      # browser/node tests skip when the toolchain is absent
python3 tools/sync_upstream.py check    # from the repo root
python3 tools/vendored_forge_tests.py   # the vendored forge suite, minus upstream packaging tests
```

`python3 auto3d.py doctor` reports the toolchain, the Codex login and which skill root is in use.

Do not report completion without reading the fresh outputs. Structural tests and
screenshot/reference-loop validation are separate required gates for visual work.

## Mandatory visual screenshot gate

For every visual reconstruction change, a readable screenshot is a hard prerequisite for any
visual claim:

1. Verify the browser/screenshot toolchain is installed and can capture the running preview.
2. Save fresh PNG/JPEG captures in the job directory, including the hero view and the orbit views.
   Inline previews alone are not evidence.
3. Read the saved screenshots back with an image-capable tool and confirm they contain the rendered
   model at the expected dimensions. A capture that cannot be opened or read is a failed gate.
4. Retain the reference/render comparison sheet, the gate JSON, and the `diagnose_render.py` output
   for that render before reporting visual validation.
5. If capture, write, readback, comparison or diagnosis fails, stop and repair the tooling. Never
   infer visual evidence from runtime readiness, structural tests or code review.
