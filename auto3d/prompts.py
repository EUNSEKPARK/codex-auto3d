"""Prompt templates for every Codex stage.

Each function returns the full text sent on `codex exec`'s stdin. The templates deliberately
repeat the pipeline's hard rules in one place: an unattended run cannot ask the user anything,
so the prompt has to say what to do at every point where the skill would otherwise stop and ask.
"""

from __future__ import annotations

from typing import Any

from .config import PASS_ORDER, VIEW_CAMERAS, VIEW_DESCRIPTIONS

# ---------------------------------------------------------------------------
# Stage 0 — prompt author
# ---------------------------------------------------------------------------

_IMAGE_RULES = """\
The image is a REFERENCE for single-image 3D reconstruction (the img2threejs pipeline rebuilds the
subject from primitives). A reference is only usable when ALL of these hold — encode each one in
the prompt explicitly:
- exactly ONE subject, fully inside the frame with roughly 8-12% empty margin on every side (never
  cropped, never touching the edges), centred, occupying most of the frame;
- a seamless, uniform, plain LIGHT-GREY studio backdrop (#e6e6e6 to #f2f2f2) with no horizon line,
  no floor props, no shadows on walls, and a very soft, faint contact shadow only;
- soft, even studio lighting (large diffuse key + fill), no rim light bloom, no lens flare, no
  fog, no motion blur, no depth-of-field, no vignette, no film grain;
- crisp silhouette; every major part and material clearly readable; no transparent or glass-heavy
  parts, no smoke/liquid/fire/lace/fur-strand effects that have no primitive reconstruction path;
- no text, no labels, no watermark, no logos, no UI, no frame/border, no split panels;
- hard-surface objects: a three-quarter view that shows the front and one side plus the top;
  characters: full body head-to-toe, neutral relaxed A-pose, feet visible, facing the camera,
  same three-quarter view unless the concept demands otherwise;
- describe the finish of each material in PBR terms (matte/satin/gloss, metal/painted metal/plastic/
  wood/fabric/rubber), because those words survive into the 3D material spec.
"""


def prompt_author(concept: str, *, style: str, profile_hint: str, complexity_hint: str, hero_camera: dict[str, float], views: list[str]) -> str:
    view_lines = "\n".join(
        f"- {view}: {VIEW_DESCRIPTIONS[view]} (camera azimuth {VIEW_CAMERAS[view]['azimuth']:.0f}°, elevation {VIEW_CAMERAS[view]['elevation']:.0f}°)"
        for view in views
    ) or "- (no extra views requested — set every view_prompts entry to null)"
    return f"""\
You are the prompt author for an automated concept → image → 3D pipeline. Do NOT generate any
image and do NOT run commands; your only output is the JSON object described by the output schema.

Concept from the user (may be Korean or English):
\"\"\"{concept.strip()}\"\"\"

Requested visual style: {style}
Profile hint: {profile_hint}  (auto = decide: 'character' for humanoid/creature figures, else 'generic')
Complexity hint: {complexity_hint}  (auto = estimate simple|moderate|complex|ultra-complex from the concept)

{_IMAGE_RULES}
Camera for the hero reference: three-quarter view, camera azimuth {hero_camera['azimuth']:.0f}° toward the
subject's own LEFT side (so the front and the subject's left side are visible), elevation
{hero_camera['elevation']:.0f}° above the subject's mid-height, 50mm-equivalent lens, no perspective distortion.
Say this in plain words inside the prompt (e.g. "three-quarter front view from the subject's left,
camera slightly above eye level").

Write `image_prompt` in English using the labeled scaffold below (one label per line). Keep the
subject faithful to the concept; add only details that make it a better 3D reference. Repeat the
mandatory constraints in the `Constraints:` and `Avoid:` lines.

Use case: stylized-concept   (or product-mockup for real products)
Asset type: 3D reconstruction reference image
Primary request: <one sentence>
Scene/backdrop: <plain light-grey seamless studio backdrop …>
Subject: <detailed subject description: parts, proportions, colours>
Style/medium: <{style} …>
Composition/framing: <single centred subject, full view, margins, camera as specified>
Lighting/mood: <soft even studio lighting …>
Color palette: <3-6 named colours>
Materials/textures: <each material in PBR words>
Constraints: <the hard rules>
Avoid: <negative list>

Extra reference views to write prompts for (same subject, identical design/colours/proportions,
same backdrop and lighting, only the camera changes — say "the exact same object as the reference
image" and describe the new camera in words):
{view_lines}

Also fill: subject_name (short English display name), subject_slug (kebab-case ASCII),
profile, complexity, camera (echo the hero camera numbers), identity_features (3-8 items the 3D
model must reproduce — silhouette shapes, part counts, colour blocks, distinctive details),
materials (PBR terms), avoid (list), notes_ko (Korean, 1-3 sentences).
Return only the JSON object.
"""


def reference_intake(
    concept: str,
    *,
    hero_camera: dict[str, float],
    camera_pinned: bool,
    supplied_views: list[str],
    profile_hint: str,
    complexity_hint: str,
) -> str:
    """Prompt for the `--reference` path: the images already exist, so this turn reads them
    instead of authoring a generation prompt. It fills the same PROMPT_SCHEMA so every later
    stage is identical to the generated-reference path."""
    attached = ["1. the hero reference (the view the render is compared against)"]
    for index, view in enumerate(supplied_views, start=2):
        attached.append(f"{index}. the {view} view — {VIEW_DESCRIPTIONS.get(view, view)}")
    camera_line = (
        f"The hero reference was captured at azimuth {hero_camera['azimuth']:.0f}°, elevation "
        f"{hero_camera['elevation']:.0f}° (the operator measured it). Echo those exact numbers in `camera`."
        if camera_pinned
        else (
            "Estimate the hero reference's camera and put it in `camera`: azimuth 0° means the camera "
            "faces the subject's front, positive azimuth walks the camera toward the subject's own LEFT "
            "(so a three-quarter view showing the front and the subject's left side is roughly +35°, the "
            "subject's left profile is +90°, the back is 180°); elevation is degrees above the subject's "
            "mid-height. Round to the nearest 5°."
        )
    )
    concept_line = (
        f"Operator's note about the subject (a hint, not the truth — the images are the truth):\n\"\"\"{concept.strip()}\"\"\"\n"
        if concept.strip()
        else ""
    )
    return f"""\
You are the intake analyst for an automated image → 3D pipeline. The reference images ALREADY
EXIST and are attached to this turn:
{chr(10).join(attached)}

Do NOT generate any image, do NOT edit any file, and do NOT run commands. Your only output is the
JSON object described by the output schema.

{concept_line}Profile hint: {profile_hint}  (auto = decide: 'character' for humanoid/creature figures, else 'generic')
Complexity hint: {complexity_hint}  (auto = estimate simple|moderate|complex|ultra-complex from what you see)

{camera_line}

Fill `image_prompt` with a faithful DESCRIPTION of the attached hero reference — not an invented
scene. It is kept as the record of what the model is being asked to rebuild, and it is what a
missing view would be regenerated from, so describe only what is actually visible, using the same
labeled scaffold (one label per line):

Use case: supplied-reference
Asset type: 3D reconstruction reference image
Primary request: <one sentence naming the subject>
Scene/backdrop: <the actual backdrop of the supplied image>
Subject: <parts, proportions, colours — read them off the image, count what can be counted>
Style/medium: <the actual rendering style of the supplied image>
Composition/framing: <how the subject sits in the frame, and the camera as given above>
Lighting/mood: <the actual lighting>
Color palette: <3-6 named colours sampled from the image>
Materials/textures: <each visible material in PBR words: matte/satin/gloss, plastic/painted metal/…>
Constraints: <what the rebuild must preserve>
Avoid: <what must not be invented>

Set every `view_prompts` entry to null for a view that is attached (it already exists); write a
prompt only for a view that is missing and would help.

Also fill: subject_name (short English display name), subject_slug (kebab-case ASCII), profile,
complexity, identity_features (3-8 things the 3D model must reproduce for the result to read as
this exact subject — silhouette shapes, part counts, colour blocks, distinctive markings),
materials (PBR terms), avoid (list), notes_ko (Korean, 1-3 sentences on what will be hard to
reconstruct from these views).

Be precise about anything the reconstruction can get wrong: how many limbs and of what shape,
whether parts are separate or fused, where a marking sits, what is symmetric and what is not.
Return only the JSON object.
"""


def template_prompt(concept: str, *, style: str, hero_camera: dict[str, float], views: list[str]) -> dict[str, Any]:
    """Deterministic, LLM-free fallback for --prompt-author template."""
    subject = concept.strip()
    prompt = "\n".join(
        [
            "Use case: stylized-concept",
            "Asset type: 3D reconstruction reference image",
            f"Primary request: a single {subject}, shown as a clean reference for 3D modelling",
            "Scene/backdrop: seamless uniform plain light-grey studio backdrop (#eeeeee), no horizon line, no floor props, only a faint soft contact shadow",
            f"Subject: {subject}; every major part clearly separated and readable; consistent proportions; the whole subject visible",
            f"Style/medium: {style}",
            f"Composition/framing: exactly one subject centred, fully inside the frame with about 10% empty margin on every side, three-quarter front view from the subject's left (camera azimuth {hero_camera['azimuth']:.0f}°), camera {hero_camera['elevation']:.0f}° above mid-height, 50mm-equivalent lens",
            "Lighting/mood: soft even studio lighting, large diffuse key light and fill, no rim bloom, no lens flare, no fog, no motion blur, no depth-of-field, no vignette",
            "Color palette: a small set of clearly distinct flat colours",
            "Materials/textures: matte or satin painted surfaces, clearly readable material boundaries, no transparency, no glass",
            "Constraints: one subject only; crisp silhouette; no text, labels, watermark, logos, UI, borders or split panels; no cropping",
            "Avoid: multiple objects, busy background, reflections on the floor, smoke, liquid, fire, lace, transparent parts, extreme perspective",
        ]
    )
    view_prompts = {name: None for name in VIEW_DESCRIPTIONS}
    for view in views:
        view_prompts[view] = (
            f"The exact same {subject} as the reference image — identical design, colours, proportions and materials — "
            f"rendered from a {VIEW_DESCRIPTIONS[view]}. Same seamless light-grey studio backdrop, same soft even lighting, "
            "single subject centred with ~10% margin, no text, no watermark."
        )
    return {
        "subject_name": subject[:60],
        "subject_slug": "",
        "profile": "generic",
        "complexity": "moderate",
        "image_prompt": prompt,
        "view_prompts": view_prompts,
        "camera": dict(hero_camera),
        "identity_features": [],
        "materials": [],
        "avoid": ["text", "watermark", "multiple objects", "busy background"],
        "notes_ko": "LLM 없이 템플릿으로 작성한 프롬프트입니다.",
    }


# ---------------------------------------------------------------------------
# Stage 1 — image generation through the built-in image_gen tool
# ---------------------------------------------------------------------------


def imagegen_turn(image_prompt: str, *, out_rel: str, size: str, quality: str, retry_note: str | None = None) -> str:
    retry = f"\nPrevious attempt feedback (fix this): {retry_note}\n" if retry_note else ""
    return f"""\
Use the $imagegen skill in its default BUILT-IN tool mode (the `image_gen` tool). Generate exactly
ONE image — no variants, no edits, no extra assets — from the prompt below. Do not rewrite the
prompt beyond the skill's normal labeled normalisation; keep every constraint. Preferred output:
{size}, quality {quality}, PNG.
{retry}
PROMPT
------
{image_prompt.strip()}
------

After the tool returns, copy the generated PNG into the workspace (create directories, overwrite
if present, keep it PNG). Exact workspace-relative destination:
TARGET: {out_rel}
Then verify the file exists and is a real PNG (e.g. `ls -l` and `file`). Never leave the asset only
under $CODEX_HOME/generated_images. Do not run any other tools or commands.

Final answer: only the JSON object described by the output schema, with saved_path="{out_rel}"
when the copy succeeded, generated=true/false, model, size, prompt_used and notes.
"""


def view_turn(view: str, view_prompt: str, *, out_rel: str, size: str, quality: str) -> str:
    return f"""\
Using the same $imagegen built-in `image_gen` tool, generate ONE more image of the SAME subject as
the reference image you generated earlier in this thread (use it as the reference/edit input so the
design, colours, proportions and materials stay identical). Only the camera changes:
{VIEW_DESCRIPTIONS[view]}.

PROMPT
------
{view_prompt.strip()}
------

Preferred output: {size}, quality {quality}, PNG. Copy the result to this exact workspace-relative path:
TARGET: {out_rel}
Verify the file exists and is a real PNG. Final answer: only the JSON object from the output schema with
saved_path="{out_rel}".
"""


# ---------------------------------------------------------------------------
# Stage 2/3 — img2threejs build turns
# ---------------------------------------------------------------------------


def _pass_list(target_pass: str) -> str:
    wanted = PASS_ORDER[: PASS_ORDER.index(target_pass) + 1]
    return " → ".join(wanted)


def build_start_turn(
    *,
    job_rel: str,
    subject_name: str,
    concept: str,
    profile: str,
    complexity: str,
    target_pass: str,
    reference_rel: str,
    extra_views: dict[str, str],
    camera: dict[str, float],
    identity_features: list[str],
    materials: list[str],
    max_per_pass: int,
    max_total: int,
    target_triangles: int,
    factory_rel: str,
    language: str,
) -> str:
    views_block = "\n".join(f"  - {name}: {path} (camera azimuth {VIEW_CAMERAS[name]['azimuth']:.0f}°, elevation {VIEW_CAMERAS[name]['elevation']:.0f}°)" for name, path in extra_views.items())
    if not views_block:
        views_block = "  - none (single view; report hidden-side confidence honestly instead of inventing detail)"
    features = "\n".join(f"  - {item}" for item in identity_features) or "  - (derive from the image)"
    mats = ", ".join(materials) or "(derive from the image)"
    character_flags = " --character" if profile == "character" else ""
    return f"""\
You are running the img2threejs skill UNATTENDED inside an automated pipeline. The skill root is
the current working directory: read ./SKILL.md now and follow it — the mandatory local state gate,
image analysis, pre-spec assessment, detail inventory, spec authoring, strict validation and
pass-locked generation. Read the grimoire files SKILL.md names at the moment you reach each stage.

## Job
- Subject: "{subject_name}" — concept: {concept.strip()}
- Reference image (attached, also on disk): {reference_rel}
  It was GENERATED for this job with a known camera: azimuth {camera['azimuth']:.0f}° toward the subject's own left
  (+X), elevation {camera['elevation']:.0f}°, ~50 mm lens, subject centred. Use `forge/stage1_intake/solve_camera_pose.py
  {reference_rel} --yaw {camera['azimuth']:.0f} --pitch {camera['elevation']:.0f} --out {job_rel}/reference-camera.json` and carry the
  block into the spec's `referenceCamera`.
- Extra reference views on disk:
{views_block}
- Profile: {profile}   (state.py --profile {profile}{'; pass --character to the assessment and spec scripts' if profile == 'character' else ''})
- Complexity estimate: {complexity} (re-judge after looking at the image; use --complexity accordingly)
- Identity-defining features the model must reproduce:
{features}
- Materials named at prompt time (PBR terms): {mats}
- Intended use: real-time browser prop, animation-ready hierarchy. performanceBudget.targetTriangles = {target_triangles}.

## Fixed paths (write EVERYTHING under the job directory; never write elsewhere)
- Job directory: {job_rel}/
- Local state: {job_rel}/.img2threejs/state.json
- Assessment: {job_rel}/assessment.json   · detail inventory: {job_rel}/detail-inventory/ and {job_rel}/detail-inventory.json
- Spec: {job_rel}/object-sculpt-spec.json
- Factory (TypeScript): {factory_rel}
- Scratch (crops, evidence, notes): {job_rel}/evidence/

## Commands you must use (run from the skill root)
python3 forge/state.py init --state {job_rel}/.img2threejs/state.json --reference {reference_rel} --profile {profile} --spec {job_rel}/object-sculpt-spec.json --max-per-pass {max_per_pass} --max-total {max_total}
python3 forge/next.py --state {job_rel}/.img2threejs/state.json
python3 forge/stage1_intake/probe_image.py {reference_rel}
python3 forge/stage1_intake/check_reference_admission.py {reference_rel} --json
python3 forge/stage2_spec/new_pre_spec_assessment.py "{subject_name}" --image {reference_rel} --complexity <tier>{character_flags} --out {job_rel}/assessment.json
python3 forge/stage1_intake/build_detail_inventory.py {reference_rel} --mode grid-3x3 --out-dir {job_rel}/detail-inventory --out {job_rel}/detail-inventory.json
python3 forge/stage2_spec/new_sculpt_spec.py "{subject_name}" --image {reference_rel} --assessment {job_rel}/assessment.json{character_flags} --out {job_rel}/object-sculpt-spec.json
python3 forge/stage2_spec/validate_sculpt_spec.py {job_rel}/object-sculpt-spec.json --strict-quality
python3 forge/stage3_build/orchestrate_passes.py status {job_rel}/object-sculpt-spec.json
python3 forge/stage3_build/generate_threejs_factory.py {job_rel}/object-sculpt-spec.json --out {factory_rel} --force
python3 forge/state.py mark <step-id> --state {job_rel}/.img2threejs/state.json --evidence <path>

## What to do in THIS turn
1. Initialise the local state, then run `forge/next.py --state …` and obey it at every step.
2. Analyse the attached image with your own vision first (grimoire/intake/image_analysis.md), then
   run the probe/admission scripts. The image was generated to be admissible; if admission still
   fails, continue anyway and record the reasons in the assessment notes.
3. Write the assessment, the detail inventory (map every detail to a component/material entry),
   and the ObjectSculptSpec. Replace the generic starter `featureReviewTargets` with the real
   identity-defining systems listed above (≤5 critical, ≤3 important per pass). Set
   `objectClass.primaryDomain`, `topologyClass` per component, materials from the image, sockets and
   pivots for anything that should move, and `referenceCamera` from the solved camera block.
4. Validate, then strict-validate. Fix the spec until `--strict-quality` passes — a BLOCKED report
   means the spec is too shallow, never that you should lower the bar.
5. Generate the `blockout` factory to {factory_rel} (`--force` is fine for a fresh file).
6. Mark every completed checklist step with evidence. Skip a non-applicable step only with
   `--status skipped --reason "..."`.
7. STOP and return the JSON below. Do NOT render, screenshot, or review in this turn — the
   pipeline renders the factory for you and comes back with a comparison sheet in the next turn.

## Unattended rules
- No human is available: never ask a question, never wait. Where the skill says
  `request-input`, make the most reasonable conservative choice, record it (assessment notes or
  approximationNotes), and continue. Use hidden-side mirroring, never invented detail.
- Do not install anything and do not use the network; everything needed is local.
- Never edit files under vendor/ — forge/, grimoire/, docs/, skills/ and scripts/ are symlinks
  into the vendored skill and are read-only. Only the job directory changes.
- Keep going through {_pass_list(target_pass)}; the target pass is `{target_pass}`.

## Final answer (this turn)
Only the JSON object from the output schema: stage="factory-ready", pass_id="blockout",
factory_path="{factory_rel}", spec_path="{job_rel}/object-sculpt-spec.json", factory_function
(the exported create…Model name), review=null, state_status (the LOCAL_STATE line from next.py),
corrections_used=0, changed_files, message, message_ko{' (Korean)' if language == 'ko' else ''}.
"""


def review_turn(
    *,
    job_rel: str,
    pass_id: str | None,
    target_pass: str,
    capture: dict[str, Any],
    gates_summary: str,
    factory_rel: str,
    turn_index: int,
    turns_left: int,
    corrections_left: int | None,
    reference_rel: str,
    final: bool = False,
) -> str:
    captures = capture.get("captures", {})
    lines = []
    for name in ("hero", "orbit-plus35", "orbit-minus35", "profile", "rear", "az090", "az270", "head-hero", "head-threequarter"):
        if name in captures:
            lines.append(f"  - {name}: {captures[name]['path']}  (azimuth {captures[name]['azimuth']:.0f}°, elevation {captures[name]['elevation']:.0f}°)")
    capture_lines = "\n".join(lines) or "  - (no captures — see errors)"
    console_errors = capture.get("consoleErrors") or []
    console_block = "\n".join(f"  - {error}" for error in console_errors[:8]) or "  - none"
    budget_line = (
        f"Review turns left after this one: {turns_left}. Corrections left (from local state): "
        f"{corrections_left if corrections_left is not None else 'see next.py'}."
    )
    if final:
        budget_line += (
            "\nTHIS IS THE FINAL REVIEW TURN: do NOT generate or edit any factory. Record the review "
            "honestly, sync state, and return stage=\"done\" only if the target pass now has a `continue` "
            "review, otherwise stage=\"blocked\" with reason \"budget\"."
        )
    map_stripped = captures.get("hero-mapstripped", {}).get("path")
    map_stripped_line = (
        f"   For the blockout pass add `--map-stripped-render {map_stripped}` (texture maps disabled) — Tier-1 requires it."
        if map_stripped
        else ""
    )
    return f"""\
RENDER RESULT for the factory {factory_rel} (pass `{pass_id or 'unknown'}`), review turn {turn_index}.
The pipeline built the TypeScript factory, rendered it in headless Chromium and captured:
{capture_lines}
- Comparison sheet (reference vs hero render, attached as an image): {capture.get('comparisonSheet')}
- Render manifest: {capture.get('renderManifest')}   · meshes export: {capture.get('meshes')}
- Browser console errors:
{console_block}

Deterministic gates already run by the pipeline (JSON files are next to the captures):
{gates_summary}

## Do now
1. Look at the attached comparison sheet with your own vision (open the PNG files too if you
   need a closer look, e.g. the rear/profile captures). Judge layer by layer per
   grimoire/feedback/render_capture.md: silhouette/proportion, component structure, form detail,
   material/surface, lighting/camera; score every critical feature. A gate failure above blocks
   `continue` even when the global score looks fine.
2. Record the deterministic Tier-1 result into the spec:
   python3 forge/stage4_review/diagnose_render.py --reference {reference_rel} --render {captures.get('hero', {}).get('path', '<hero.png>')} --spec {job_rel}/object-sculpt-spec.json --pass-id {pass_id or '<pass>'} --in-place --json
{map_stripped_line}
   python3 forge/stage3_build/orchestrate_passes.py check {job_rel}/object-sculpt-spec.json --pass-id {pass_id or '<pass>'}
3. Record your review (all evidence flags are required for `continue`; `--feature-reviews-json`
   must score EVERY critical feature target of this pass by its `id`, with `visible: true`):
   python3 forge/stage4_review/append_review.py {job_rel}/object-sculpt-spec.json --pass-id {pass_id or '<pass>'} \\
     --fidelity <0-1> --action <continue|refine-spec|refine-code|stop> --summary "..." \\
     --reference-screenshot {reference_rel} --render-screenshot {captures.get('hero', {}).get('path', '<hero.png>')} \\
     --comparison-image {capture.get('comparisonSheet')} --camera-view three-quarter \\
     --ai-vision-score <0-1> --layer-scores-json '{{"silhouetteProportion":..,"componentStructure":..,"formDetail":..,"materialSurface":..,"lightingCamera":..}}' \\
     --feature-reviews-json '[{{"id":"<featureReviewTarget id>","score":<0-1>,"visible":true,"notes":"..."}}, ...]' \\
     --ai-vision-notes "matched: ...; mismatches: ...; root cause: ..." --mismatches "a; b" --in-place
   {('For the blockout pass also pass `--map-stripped-render ' + str(map_stripped) + '`.') if map_stripped else ''}
   Then `python3 forge/stage3_build/orchestrate_passes.py sync {job_rel}/object-sculpt-spec.json --in-place`, mark the
   checklist evidence with state.py, and re-run `forge/next.py --state {job_rel}/.img2threejs/state.json`.
4. Decide exactly one action:
   - continue  → if `{pass_id}` == `{target_pass}`: you are DONE (stage="done"). Otherwise generate
     the NEXT unlocked pass with `generate_threejs_factory.py … --out {factory_rel} --force`
     (carry any hand refinement back into the spec first) and return stage="factory-ready".
   - refine-spec → fix the spec, re-validate strict, regenerate the same pass with --force,
     return stage="factory-ready".
   - refine-code → edit {factory_rel} directly (do not regenerate), return stage="factory-ready".
   - stop / hard stop from next.py (exit 3) → return stage="blocked" with the reason.
{budget_line} When no corrections remain, record the best available review and return
stage="blocked" (budget) or stage="done" only if the target pass genuinely passed.

Never claim a pass `continue` without the comparison sheet and gate evidence; never render
anything yourself — return stage="factory-ready" and the pipeline renders again.

## Final answer
Only the JSON object from the output schema (stage, pass_id = the pass the CURRENT factory file
represents, factory_path, spec_path, factory_function, review = the entry you just recorded,
state_status, corrections_used, changed_files, message, message_ko).
"""


def wrapup_turn(*, job_rel: str, reason: str) -> str:
    return f"""\
The pipeline is stopping this reconstruction now: {reason}.
Do not render or generate anything further. If a review for the last rendered pass has not been
recorded yet, record it honestly with append_review.py (action=stop or the real decision), run
`orchestrate_passes.py sync {job_rel}/object-sculpt-spec.json --in-place`, mark checklist evidence,
and run `forge/next.py --state {job_rel}/.img2threejs/state.json` once more.
Final answer: only the JSON object from the output schema with stage="blocked" (or "done" if the
target pass already has a `continue` review), the latest review entry, state_status, and a short
message/message_ko summarising what was achieved and what still does not match the reference.
"""
