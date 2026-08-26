#!/usr/bin/env python3
"""A stand-in for the `codex` binary so the orchestration can be exercised without an OpenAI login.

It speaks just enough of the `codex exec` surface the orchestrator uses (flags, stdin prompt,
`--json` events, `-o` last message, `resume <id>`) and reacts to the prompt templates:

- prompt author  → returns a valid PROMPT_SCHEMA object
- $imagegen      → writes a synthetic PNG to the requested path
- build start    → runs the real forge scripts (state init, assessment, starter spec, factory)
- review turn    → appends a `continue` review to the spec and regenerates the next pass, or `done`

Set FAKE_CODEX_MODE to change behaviour: `nojson` (final message is prose), `blocked` (reviews
return blocked), `noimage` (imagegen does not write the file), `slow` (sleeps 5 s).
"""

from __future__ import annotations

import json
import os
import re
import struct
import subprocess
import sys
import time
import uuid
import zlib
from pathlib import Path

REPO_ROOT = Path(os.environ.get("FAKE_CODEX_REPO") or Path(__file__).resolve().parents[2])
MODE = os.environ.get("FAKE_CODEX_MODE", "")
PASS_ORDER = ["blockout", "structural-pass", "form-refinement", "material-pass", "surface-pass", "lighting-pass", "interaction-pass", "optimization-pass"]


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def write_png(path: Path, width: int = 768, height: int = 768, variant: str = "hero") -> None:
    """Light-grey backdrop with a dark red 'robot' — coherent silhouette covering roughly 30% of
    the frame so check_reference_admission.py admits it. Extra views use a clearly different
    composition so the pHash duplicate gate does not reject them as copies of the hero."""
    shapes: list[tuple[str, float, float, float, float, tuple[int, int, int]]]
    if variant == "hero":
        shapes = [("rect", 0.5, 0.56, 0.22, 0.24, (180, 40, 40)), ("circle", 0.5, 0.24, 0.13, 0.13, (200, 60, 60)),
                  ("rect", 0.5, 0.10, 0.01, 0.04, (60, 60, 60)), ("rect", 0.24, 0.56, 0.04, 0.05, (90, 90, 100)), ("rect", 0.76, 0.56, 0.04, 0.05, (90, 90, 100))]
    elif variant == "side":
        shapes = [("rect", 0.5, 0.58, 0.12, 0.26, (150, 35, 35)), ("circle", 0.5, 0.24, 0.10, 0.10, (200, 60, 60)), ("rect", 0.62, 0.58, 0.05, 0.05, (90, 90, 100))]
    elif variant == "top":
        shapes = [("circle", 0.5, 0.5, 0.20, 0.20, (200, 60, 60)), ("rect", 0.5, 0.5, 0.30, 0.10, (90, 90, 100))]
    else:  # front / back: wide stance, different head placement and proportions
        shapes = [("rect", 0.5, 0.62, 0.28, 0.18, (170, 40, 40)), ("circle", 0.5, 0.30, 0.16, 0.16, (200, 60, 60)),
                  ("rect", 0.18, 0.62, 0.05, 0.16, (90, 90, 100)), ("rect", 0.82, 0.62, 0.05, 0.16, (90, 90, 100)),
                  ("rect", 0.40, 0.86, 0.05, 0.06, (60, 60, 60)), ("rect", 0.60, 0.86, 0.05, 0.06, (60, 60, 60))]
    import math

    seed = sum(ord(ch) for ch in variant)
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            # gentle vignette + texture so the perceptual hash is not degenerate (flat images
            # collapse to the same DCT signature and the duplicate gate fires spuriously)
            shade = int(6 * math.sin((x + seed) * 0.05) + 6 * math.cos((y - seed) * 0.037))
            r, g, b = 236 + shade, 236 + shade, 236 + shade
            for kind, cx, cy, hw, hh, colour in shapes:
                px, py = cx * width, cy * height
                inside = (kind == "rect" and abs(x - px) < hw * width and abs(y - py) < hh * height) or (
                    kind == "circle" and (x - px) ** 2 + (y - py) ** 2 < (hw * width) ** 2
                )
                if inside:
                    light = int(25 * (1 - (y - py + hh * height) / max(1.0, 2 * hh * height))) + int(8 * math.sin(x * 0.2))
                    r, g, b = (max(0, min(255, c + light)) for c in colour)
                    break
            row += bytes((r, g, b))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)
    emit({"type": "item.completed", "item": {"id": f"item_{uuid.uuid4().hex[:6]}", "type": "command_execution", "command": " ".join(cmd), "aggregated_output": (completed.stdout + completed.stderr)[-500:], "exit_code": completed.returncode, "status": "completed"}})
    return completed


def finish(message: dict | str, last_path: Path | None, usage: dict) -> int:
    text = message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)
    if MODE == "nojson" and isinstance(message, dict):
        text = "I did the work. Here is a summary in prose without JSON."
    emit({"type": "item.completed", "item": {"id": "msg", "type": "agent_message", "text": text}})
    emit({"type": "turn.completed", "usage": usage})
    if last_path is not None:
        last_path.parent.mkdir(parents=True, exist_ok=True)
        last_path.write_text(text, encoding="utf-8")
    return 0


def handle_prompt_author(prompt: str, last: Path | None) -> int:
    views = {"front": None, "side": None, "back": None, "top": None}
    for view in views:
        if f"- {view}:" in prompt:
            views[view] = f"The exact same red toy robot as the reference image, {view} view, plain light-grey backdrop."
    concept = re.search(r'"""(.*?)"""', prompt, re.DOTALL)
    return finish(
        {
            "subject_name": "Red Toy Robot",
            "subject_slug": "red-toy-robot",
            "profile": "character" if "character" in (concept.group(1) if concept else "").lower() else "generic",
            "complexity": "moderate",
            "image_prompt": "Use case: stylized-concept\nAsset type: 3D reconstruction reference image\nPrimary request: a single red toy robot\nScene/backdrop: plain light-grey seamless studio backdrop\nSubject: boxy red toy robot with round head, antenna, two arms\nStyle/medium: stylized 3D render\nComposition/framing: single centred subject, three-quarter front view from the subject's left, camera slightly above eye level, 10% margin\nLighting/mood: soft even studio lighting\nColor palette: red, dark grey, off-white\nMaterials/textures: matte painted plastic, satin dark-grey rubber\nConstraints: one subject, no text, no watermark\nAvoid: busy background, multiple objects",
            "view_prompts": views,
            "camera": {"azimuth": 35, "elevation": 15},
            "identity_features": ["boxy torso", "round head with antenna", "two cylindrical arms"],
            "materials": ["matte painted plastic", "satin rubber"],
            "avoid": ["text", "watermark"],
            "notes_ko": "3D 복원에 적합하도록 단일 피사체, 중립 배경, 3/4 뷰로 작성했습니다.",
        },
        last,
        {"input_tokens": 1200, "cached_input_tokens": 0, "output_tokens": 400},
    )


def handle_imagegen(prompt: str, last: Path | None) -> int:
    match = re.search(r"^TARGET: (\S+)", prompt, re.MULTILINE)
    if not match:
        return finish({"saved_path": None, "generated": False, "model": "gpt-image-2", "size": "1024x1024", "prompt_used": prompt[:200], "notes": "no path"}, last, {"input_tokens": 500, "output_tokens": 50})
    rel = match.group(1).strip()
    target = REPO_ROOT / rel
    variant = Path(rel).stem if Path(rel).stem in {"front", "side", "back", "top"} else "hero"
    if MODE != "noimage":
        write_png(target, variant=variant)
        emit({"type": "item.completed", "item": {"id": "img", "type": "image_generation", "status": "completed"}})
        run(["ls", "-l", str(target)])
    return finish({"saved_path": rel, "generated": MODE != "noimage", "model": "gpt-image-2", "size": "1024x1024", "prompt_used": prompt.split("PROMPT")[-1][:400], "notes": None}, last, {"input_tokens": 900, "cached_input_tokens": 0, "output_tokens": 120})


def _field(prompt: str, pattern: str) -> str | None:
    match = re.search(pattern, prompt)
    return match.group(1).strip() if match else None


def handle_build_start(prompt: str, last: Path | None) -> int:
    job_rel = _field(prompt, r"- Job directory: (\S+?)/\n") or _field(prompt, r"- Job directory: (\S+)")
    job_rel = (job_rel or "").rstrip("/")
    reference = _field(prompt, r"- Reference image \(attached, also on disk\): (\S+)")
    profile = _field(prompt, r"- Profile: (\w+)") or "generic"
    subject = _field(prompt, r'- Subject: "(.*?)"') or "Subject"
    factory = _field(prompt, r"- Factory \(TypeScript\): (\S+)")
    target = _field(prompt, r"the target pass is `([\w-]+)`") or "material-pass"
    max_per = _field(prompt, r"--max-per-pass (\d+)") or "3"
    max_total = _field(prompt, r"--max-total (\d+)") or "6"
    job = REPO_ROOT / job_rel
    state = job / ".img2threejs" / "state.json"
    spec = job / "object-sculpt-spec.json"
    py = sys.executable
    run([py, "forge/state.py", "init", "--state", str(state), "--reference", reference or "", "--profile", profile, "--spec", str(spec), "--max-per-pass", max_per, "--max-total", max_total])
    run([py, "forge/next.py", "--state", str(state)])
    run([py, "forge/stage1_intake/probe_image.py", reference or ""])
    run([py, "forge/stage2_spec/new_pre_spec_assessment.py", subject, "--image", reference or "", "--complexity", "moderate", "--out", str(job / "assessment.json"), "--force"])
    run([py, "forge/stage2_spec/new_sculpt_spec.py", subject, "--image", reference or "", "--assessment", str(job / "assessment.json"), "--out", str(spec), "--force"])
    run([py, "forge/stage2_spec/validate_sculpt_spec.py", str(spec)])
    if factory:
        run([py, "forge/stage3_build/generate_threejs_factory.py", str(spec), "--out", str(REPO_ROOT / factory), "--force", "--allow-nonstrict"])
    fn = None
    if factory and (REPO_ROOT / factory).is_file():
        m = re.search(r"export function (create\w+Model)\(", (REPO_ROOT / factory).read_text())
        fn = m.group(1) if m else None
    if MODE == "slow":
        time.sleep(5)
    return finish(
        {
            "stage": "factory-ready",
            "pass_id": "blockout",
            "factory_path": factory,
            "spec_path": f"{job_rel}/object-sculpt-spec.json",
            "factory_function": fn,
            "review": None,
            "state_status": "LOCAL_STATE status=active step=generate-blockout pass=blockout loop=0/3 total=0/6",
            "corrections_used": 0,
            "changed_files": [f"{job_rel}/assessment.json", f"{job_rel}/object-sculpt-spec.json", factory or ""],
            "message": f"Starter spec + blockout factory generated (fake). target={target}",
            "message_ko": "가짜 Codex: 스펙과 blockout 팩토리를 생성했습니다.",
        },
        last,
        {"input_tokens": 30000, "cached_input_tokens": 12000, "output_tokens": 6000},
    )


def handle_review(prompt: str, last: Path | None) -> int:
    factory = _field(prompt, r"RENDER RESULT for the factory (\S+) \(pass `([\w-]+)`\)")
    match = re.search(r"RENDER RESULT for the factory (\S+) \(pass `([\w-]+)`\)", prompt)
    factory, pass_id = (match.group(1), match.group(2)) if match else (factory, "blockout")
    target = _field(prompt, r"if `[\w-]+` == `([\w-]+)`") or "material-pass"
    job_rel = _field(prompt, r"python3 forge/stage4_review/append_review.py (\S+)/object-sculpt-spec.json")
    spec_path = REPO_ROOT / f"{job_rel}/object-sculpt-spec.json"
    final = "FINAL REVIEW TURN" in prompt
    hero = _field(prompt, r"- hero: (\S+)")
    cmp = _field(prompt, r"attached as an image\): (\S+)")
    review = {"pass_id": pass_id, "action": "continue", "fidelity": 0.82, "ai_vision_score": 0.81, "notes": "fake review: silhouette matches"}
    if MODE == "blocked":
        return finish({"stage": "blocked", "pass_id": pass_id, "factory_path": factory, "spec_path": f"{job_rel}/object-sculpt-spec.json", "factory_function": None, "review": None, "state_status": "STOP", "corrections_used": 0, "changed_files": [], "message": "blocked (fake)", "message_ko": "차단됨(가짜)"}, last, {"input_tokens": 2000, "output_tokens": 200})
    if spec_path.is_file():
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        history = spec.setdefault("reviewHistory", [])
        history.append(
            {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "passId": pass_id,
                "estimatedFidelity": 0.82,
                "aiVisionScore": 0.81,
                "visualAcceptanceThreshold": 0.7,
                "layerScores": {"silhouetteProportion": 0.85, "componentStructure": 0.8, "formDetail": 0.75, "materialSurface": 0.7, "lightingCamera": 0.8},
                "featureReviews": [],
                "action": "continue",
                "summary": f"fake review of {pass_id}",
                "matched": ["silhouette"],
                "mismatches": [],
                "specFixes": [],
                "codeFixes": [],
                "evidence": [hero or "", cmp or ""],
                "visualEvidence": {"renderScreenshot": hero, "comparisonImage": cmp, "referenceScreenshot": None},
            }
        )
        spec_path.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        run([sys.executable, "forge/stage3_build/orchestrate_passes.py", "sync", str(spec_path), "--in-place"])
    if pass_id == target or final:
        stage = "done" if pass_id == target else "blocked"
        return finish({"stage": stage, "pass_id": pass_id, "factory_path": factory, "spec_path": f"{job_rel}/object-sculpt-spec.json", "factory_function": None, "review": review, "state_status": "LOCAL_STATE status=complete", "corrections_used": 0, "changed_files": [f"{job_rel}/object-sculpt-spec.json"], "message": f"{pass_id} reviewed continue (fake)", "message_ko": f"{pass_id} 리뷰 완료(가짜)"}, last, {"input_tokens": 4000, "cached_input_tokens": 3000, "output_tokens": 500})
    next_pass = PASS_ORDER[PASS_ORDER.index(pass_id) + 1] if pass_id in PASS_ORDER[:-1] else pass_id
    if factory and spec_path.is_file():
        run([sys.executable, "forge/stage3_build/generate_threejs_factory.py", str(spec_path), "--out", str(REPO_ROOT / factory), "--force", "--allow-nonstrict"])
        # make the file differ so the orchestrator sees a new hash
        path = REPO_ROOT / factory
        path.write_text(path.read_text(encoding="utf-8") + f"\n// fake pass {next_pass}\n", encoding="utf-8")
    return finish({"stage": "factory-ready", "pass_id": next_pass, "factory_path": factory, "spec_path": f"{job_rel}/object-sculpt-spec.json", "factory_function": None, "review": review, "state_status": f"LOCAL_STATE status=active pass={next_pass}", "corrections_used": 0, "changed_files": [factory or ""], "message": f"{pass_id} continue → generated {next_pass} (fake)", "message_ko": f"{pass_id} 통과, {next_pass} 생성(가짜)"}, last, {"input_tokens": 5000, "cached_input_tokens": 4000, "output_tokens": 700})


def main(argv: list[str]) -> int:
    if argv[:1] == ["--version"]:
        print("codex-cli 0.0.0-fake")
        return 0
    if argv[:2] == ["login", "status"]:
        print("Logged in using ChatGPT (fake)")
        return 0
    if argv[:1] != ["exec"]:
        print(f"fake codex: unsupported {argv}", file=sys.stderr)
        return 2
    args = argv[1:]
    resume_id = None
    if args[:1] == ["resume"]:
        resume_id = args[1]
        args = args[2:]
    last: Path | None = None
    images: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in {"-o", "--output-last-message"}:
            last = Path(args[i + 1]); i += 2; continue
        if arg in {"-i", "--image"}:
            images.append(args[i + 1]); i += 2; continue
        if arg in {"-c", "-m", "--sandbox", "-C", "--output-schema"}:
            i += 2; continue
        i += 1
    prompt = sys.stdin.read()
    thread = resume_id or str(uuid.uuid4())
    emit({"type": "thread.started", "thread_id": thread})
    emit({"type": "turn.started"})
    for image in images:
        if not Path(image).is_file():
            emit({"type": "error", "message": f"attached image missing: {image}"})
    if "You are the prompt author" in prompt:
        return handle_prompt_author(prompt, last)
    if "$imagegen" in prompt:
        return handle_imagegen(prompt, last)
    if "You are running the img2threejs skill UNATTENDED" in prompt:
        return handle_build_start(prompt, last)
    if "RENDER RESULT" in prompt:
        return handle_review(prompt, last)
    if "not the required JSON" in prompt:
        return finish({"stage": "blocked", "pass_id": None, "factory_path": None, "spec_path": None, "factory_function": None, "review": None, "state_status": None, "corrections_used": 0, "changed_files": [], "message": "nudged", "message_ko": "재요청"}, last, {"input_tokens": 100, "output_tokens": 50})
    return finish("Hello from fake codex", last, {"input_tokens": 10, "output_tokens": 5})


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
