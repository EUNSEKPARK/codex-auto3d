# codex-auto3d — unattended concept → Codex image → img2threejs model

> 한국어 가이드: [README.ko.md](README.ko.md)

An orchestrator that drives the [Codex CLI](https://developers.openai.com/codex) through the whole
img2threejs loop without a human in the seat:

```
concept text ─▶ 1. prompt author (codex exec, read-only)
             ─▶ 2. reference image (Codex built-in image_gen / gpt-image-2, admission-gated)
             ─▶ 3. img2threejs build loop (Codex judges + writes; Python bundles, renders, captures, gates)
             ─▶ 4. report.html per job + gallery
```

The output is what img2threejs promises — a **code-only procedural Three.js model** (TypeScript
factory + `ObjectSculptSpec`) — plus an interactive `preview.html`, turntable captures, the
reference-vs-render comparison sheet, gate results and a report. No mesh files are fetched.

## Why the loop is split this way

img2threejs already says "scripts enforce, the model judges". The orchestrator keeps that split:

- **Codex** does the judgment work: writes the 3D-friendly image prompt, generates the reference,
  analyses it, authors the spec, decides `continue | refine-spec | refine-code` from the comparison
  sheet, and edits code. Every turn ends in a JSON message (`--output-schema`) so the orchestrator
  can act on it.
- **Python (stdlib)** does the deterministic work: esbuild bundle → headless Chromium capture of
  12 views (hero, ±35°, profile, rear, 0/90/180/270 turntable, top, head close-ups, map-stripped
  hero) → `make_comparison_sheet.py`, `turntable_gate.py`, `self_intersection.py` (from a mesh
  export the viewer produces in world space), `diagnose_render.py` Tier-1, `interior_difference.py`,
  `diagnose_render_multi_angle.py`, optional `tsc --noEmit` → a `render-manifest.json` in the
  `render_bridge.py` evidence format.
- The orchestrator verifies claims against disk: factory hash before/after a turn and the spec's
  `reviewHistory` decide whether a pass really passed, not the model's prose.

Because the reference is *generated*, its camera is known (azimuth 35°, elevation 15° by default);
the build prompt hands that to `solve_camera_pose.py` so the hero render and the reference share a
framing, which is what keeps the Divine Eye's scale/aspect gates meaningful.

## What is in this repository

A standalone repository: clone it and everything the pipeline needs is already here.

```
auto3d.py · auto3d/          the orchestrator (Python 3.10+, standard library only)
tests/ · examples/ · viewer/ fake-Codex test suite, batch input samples, the preview viewer
node/ · .venv/               optional toolchain installed by `auto3d.py setup` (gitignored)
tools/sync_upstream.py       vendoring tool: drift check and refresh
vendor/img2threejs/          a vendored copy of the img2threejs skill — SKILL.md, forge/,
                             grimoire/, docs/, skills/, scripts/ — pinned by VENDORED.json
forge, grimoire, docs, …     root symlinks into vendor/img2threejs, so every skill-relative
                             path in SKILL.md and in the prompts resolves from the repo root
work/                        job outputs, one directory per run (gitignored)
```

The skill is vendored rather than submoduled so a fresh clone needs no second checkout and no
network. To drive a different checkout instead — an upstream clone you are tracking, or a fork —
set `IMG2THREEJS_ROOT=/path/to/img2threejs`; `doctor` prints which one is in use.

Keeping the copy honest:

```bash
python3 tools/sync_upstream.py check                        # drift against VENDORED.json
python3 tools/sync_upstream.py update --from ../img2threejs # re-vendor and record the commit
```

`check` lists vendored files edited locally, and `update` refuses to overwrite them without
`--force`, so a local patch is never lost silently in a refresh.

## Install

```bash
# from this repository's root
python3 auto3d.py setup --link-skill   # node runtime + Playwright/Chromium (+ skill symlinks)
python3 auto3d.py doctor               # codex login, imagegen skill, toolchain, forge
```

Requirements: Python 3.10+, Node 18+, `npm i -g @openai/codex` and `codex login` (ChatGPT plan —
the built-in image tool needs no API key). `OPENAI_API_KEY` is only needed for
`--image-backend api|auto` (stdlib `urllib` client for `/v1/images/generations` and `/edits`).

The toolchain is isolated in `node/` and `.venv/`; the vendored `forge/` core stays
dependency-free, in line with the img2threejs rules.

## Use

```bash
python3 auto3d.py run --prompt "a red toy robot with a round head and an antenna"
python3 auto3d.py run -p "storybook girl character in a yellow dress" --profile character --quality full --views front,side
python3 auto3d.py run -p "wooden toy train" --until image          # stop after the image
python3 auto3d.py resume --job work/auto3d/<job>                     # continue
python3 auto3d.py batch --file examples/prompts.example.csv
python3 auto3d.py preview --job work/auto3d/<job>                    # re-render without Codex
python3 auto3d.py preview --factory path/to/createFooModel.ts --reference ref.png
python3 auto3d.py gallery
```

### From an image you already have

When the subject already exists — concept art, a turnaround sheet, a product photo — `--reference`
skips generation and feeds your file into the same loop. The intake turn reads the image instead of
authoring a prompt for one, so it costs one Codex turn and no image credits.

```bash
# a turnaround sheet → one reference per view (cut, flatten, frame; standard library only)
python3 tools/prepare_reference.py sheet.png --split --out work/refs            # writes contact.png
python3 tools/prepare_reference.py sheet.png --split --out work/refs \
    --views front,hero,side,back,skip

python3 auto3d.py run --reference work/refs/hero.png \
    --view front=work/refs/front.png --view side=work/refs/side.png --view back=work/refs/back.png \
    --reference-camera 35,0 --profile character --quality standard
```

The first render of a job also **calibrates the camera framing against the reference**: one probe
capture, measured with the Tier-1 gate's own silhouette code, then the margin corrected so the
model fills the frame the way the subject does (bbox area goes as 1/margin²). Without it a render
sat a third under the reference's scale and the scale gate failed on framing alone, burning review
turns. It costs about 20 seconds, once per job, and is cached on the job.

`--reference` takes a PNG or JPEG; `--view NAME=PATH` (front|side|back|top) adds the other angles,
and each goes through the same admission gate as a generated reference — a view that is a
near-duplicate of the hero is dropped rather than handed to the model as an angle it is not.
`--reference-camera AZ,EL` states the hero's camera in degrees (azimuth 0 = straight on, positive
walks the camera toward the subject's own left, so a three-quarter from its left is `35,0`); leave
it out and the intake turn estimates it. A supplied hero that fails admission is a warning, not a
stop — it is your image and there is nothing to regenerate, so the reasons are recorded and the
build continues with them visible.

`tools/prepare_reference.py` is what makes delivered art usable: it cuts a sheet into figures,
flattens transparency onto the pipeline's own backdrop, and frames every figure on one canvas at
**one scale with a common baseline** — fitting each figure separately would quietly change the
subject's proportions between views and the reconstruction would inherit that error. Run it with
`--split` and no `--views` first; it writes a contact sheet and prints the command to run once you
have named the figures left to right.

`--quality` maps to the last pass that must be reviewed `continue`: `draft` → `form-refinement`,
`standard` → `material-pass`, `full` → `optimization-pass` (or pass any pass id).

Batch inputs: `.txt` (one concept per line, `concept | name`), `.csv`, `.json`, `.xlsx` (first
sheet, stdlib reader). Column names may be English or Korean (`concept/개념`, `name/이름`,
`profile/프로파일`, `quality/품질`, `views/뷰`, `complexity/복잡도`, `style/스타일`).

Settings: `auto3d.config.json` (see `auto3d.config.example.json`) < `AUTO3D_*` env < CLI flags /
`--set key=value`.

## Job layout (`work/auto3d/<stamp>-<slug>/`, gitignored)

```
report.html · report.json          summary: reference vs render, turntable, gates, review history, tokens
preview/preview.html               interactive viewer (orbit, 1-5 views, R turntable, W wireframe)
preview/captures/*.png             hero, az000/az090/rear/az270, orbit-±35, profile, top, head-*, hero-mapstripped
preview/cmp.png · gates/*.json     comparison sheet and gate outputs · history/turn-NN-cmp.png per render
preview/render-manifest.json       render_bridge evidence format · meshes.json (world-space export)
src/create<Name>Model.ts           the generated factory
object-sculpt-spec.json            spec incl. reviewHistory · .img2threejs/state.json local checklist
reference/hero.png (+views)        generated references + admission verdicts · prompt/prompt.json
codex/*.events.jsonl               full JSONL event logs per Codex turn (commands, outputs, usage)
```

## How a run negotiates with Codex

| Turn | Prompt (`auto3d/prompts.py`) | Sandbox | Ends with |
|---|---|---|---|
| prompt author | concept + reference-image rules | read-only, ephemeral | `PROMPT_SCHEMA` JSON |
| image | `$imagegen` built-in tool, copy to `reference/hero.png` | workspace-write, no network | `IMAGEGEN_SCHEMA` JSON, then `check_reference_admission.py` (≤3 attempts) |
| build start | follow `./SKILL.md`: state init → analysis → assessment → detail inventory → spec → strict validate → blockout factory | workspace-write, no network | `TURN_SCHEMA` `stage=factory-ready` |
| review N | comparison sheet attached with `-i`, gate summary, exact `append_review.py` flags | resumed thread | `factory-ready` (next pass / refined) · `done` · `blocked` |

Budgets: `max_review_turns` (default 12), `state.py --max-per-pass 5 --max-total 10`, a per-turn
timeout plus a larger `first_turn_timeout_min` for the intake/spec turn, and a per-job timeout. Exhaustion ends in a final review-only turn and a `partial`/`blocked` job, never a
silent stop. A `blocked` before any factory exists gets one automatic "deepen the spec" retry.

## Making it talk

`lipsync/` turns a line of Korean into audio plus a mouth-shape timeline, which is what a
generated model needs before it can speak.

```bash
export TYPECAST_API_KEY=...
python3 tools/lipsync.py voices --filter 진서
python3 tools/lipsync.py say --voice 65bb3a1976b69213594357fc \
    --text "안녕하세요, 오늘도 좋은 하루 보내세요." --out work/speech/greeting
```

Typecast's `/v1/text-to-speech/with-timestamps` returns the audio and per-character timings in the
same response, so there is no forced alignment step. Korean is written in syllable blocks, which
makes a character timestamp a syllable timestamp, and a syllable's mouth shape is carried by its
vowel: the 중성 maps to one of five shapes (AA/EH/OH/OO/EE), and ㅁ/ㅂ/ㅃ/ㅍ close the lips at the
head or the tail of the block. `visemes.json` holds the segments and the interpolation keys; it
names no renderer, so the same timeline can drive a morph-target rig, a 2D mouth swap or a
compositor.

## Tests

```bash
cd tests && python3 -m unittest -v      # browser/node tests skip when the toolchain is absent
python3 tools/sync_upstream.py check       # from the repo root: vendored skill matches its manifest
python3 tools/vendored_forge_tests.py      # the vendored forge suite (~1080 tests)
```

`vendored_forge_tests.py` excludes two upstream tests that assert facts about the *upstream
repository* — its `.gitignore`/git index and an optional vision integration this pipeline does not
use — and prints them with the reason. `--list-excluded` shows the list, `--all` runs them anyway.

`tests/fake_codex.py` stands in for the `codex` binary (it runs the real forge scripts for the
build turn and writes synthetic PNGs), so the orchestration, rendering, gates and reports are
exercised end to end without an OpenAI login. Browser-dependent tests skip when the toolchain is
not installed. Claims in this README about *real* Codex runs are limited to what the CLI surface
verified here (`codex exec` flags, JSONL events, `--output-schema`, `resume`, the bundled
`imagegen` skill); run `doctor` and a `run --until image` on your machine before a batch.

## Limits

- One generated image (plus optional front/side/back/top views) cannot fix hidden geometry; the
  pipeline mirrors and records confidence rather than inventing detail.
- Quality depends on the Codex model's vision judgement and spec authoring; use `--quality full`,
  `--views front,side,back` and `--reasoning-effort high` when likeness matters.
- The prompts hard-code forge script flags as of the vendored img2threejs commit recorded in
  `vendor/img2threejs/VENDORED.json` (upstream v1.5.1). After a `sync_upstream.py update`, run the
  tests and re-check `auto3d/prompts.py` against the scripts it calls.
