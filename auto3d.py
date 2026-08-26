#!/usr/bin/env python3
"""codex_auto3d — concept → Codex image → img2threejs procedural 3D model, unattended.

    python3 auto3d.py setup            # one-time: node runtime + Playwright
    python3 auto3d.py doctor           # check codex login, toolchain, skill
    python3 auto3d.py run --prompt "빨간 장난감 로봇"          # concept → image → 3D
    python3 auto3d.py run --reference art.png --view side=side.png   # your own image → 3D
    python3 auto3d.py batch --file prompts.csv
    python3 auto3d.py resume --job work/auto3d/<job>
    python3 auto3d.py preview --job work/auto3d/<job>
    python3 auto3d.py gallery

Run from this repository root. The img2threejs skill is vendored at vendor/img2threejs
(override with IMG2THREEJS_ROOT); paths in prompts are relative to this repository.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from auto3d import __version__  # noqa: E402
from auto3d.batch import parse_batch_file  # noqa: E402
from auto3d.codex import codex_home, codex_version, login_status  # noqa: E402
from auto3d.config import CONFIG_FILE, DEFAULTS, PASS_ORDER, VIEW_CAMERAS, Settings, load_settings  # noqa: E402
from auto3d.jobs import create_job, list_jobs, load_job  # noqa: E402
from auto3d.pipeline import Pipeline, describe_job  # noqa: E402
from auto3d.preview import NODE_DIR, VENV_DIR, esbuild_bin, playwright_python, render_factory, three_installed, tsc_bin  # noqa: E402
from auto3d.util import Auto3DError, PROJECT_ROOT, REPO_ROOT, SKILL_ROOT, human_duration, log, relpath, set_log_file, set_verbose, write_json  # noqa: E402


# ---------------------------------------------------------------------------
# argument plumbing
# ---------------------------------------------------------------------------


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=None, help=f"settings JSON (default {relpath(CONFIG_FILE)})")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override any setting, e.g. --set model=gpt-5.4")
    parser.add_argument("-v", "--verbose", action="store_true")


def add_run_overrides(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", choices=["auto", "generic", "character"])
    parser.add_argument("--quality", help="draft | standard | full | <pass-id>  (target build pass)")
    parser.add_argument("--complexity", choices=["auto", "simple", "moderate", "complex", "ultra-complex"])
    parser.add_argument("--style", help="visual style hint for the image prompt")
    parser.add_argument("--views", help="extra reference views, comma-separated: front,side,back,top")
    parser.add_argument("--image-backend", choices=["codex", "api", "auto"], dest="image_backend")
    parser.add_argument("--image-model", dest="image_model")
    parser.add_argument("--image-size", dest="image_size")
    parser.add_argument("--image-quality", dest="image_quality", choices=["low", "medium", "high", "auto"])
    parser.add_argument("--prompt-author", dest="prompt_author", choices=["codex", "template"])
    parser.add_argument("-m", "--model", help="Codex model override (passed as `codex -m`)")
    parser.add_argument("--reasoning-effort", dest="reasoning_effort", choices=["low", "medium", "high", "xhigh"])
    parser.add_argument("--sandbox", choices=["workspace-write", "danger-full-access"])
    parser.add_argument("--max-review-turns", dest="max_review_turns", type=int)
    parser.add_argument("--turn-timeout-min", dest="turn_timeout_min", type=int)
    parser.add_argument("--job-timeout-min", dest="job_timeout_min", type=int)
    parser.add_argument("--work-root", dest="work_root")


def overrides_from_args(args: argparse.Namespace) -> dict:
    overrides: dict = {}
    for key in DEFAULTS:
        if hasattr(args, key) and getattr(args, key) is not None:
            overrides[key] = getattr(args, key)
    if getattr(args, "views", None) is not None:
        overrides["views"] = [part.strip() for part in str(args.views).split(",") if part.strip()]
    for item in getattr(args, "set", []) or []:
        if "=" not in item:
            raise Auto3DError(f"--set expects KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        key = key.strip()
        if key not in DEFAULTS:
            raise Auto3DError(f"unknown setting {key!r}; known: {', '.join(sorted(DEFAULTS))}")
        default = DEFAULTS[key]
        try:
            if isinstance(default, bool):
                overrides[key] = value.strip().lower() in {"1", "true", "yes", "on"}
            elif isinstance(default, int):
                overrides[key] = int(value)
            elif isinstance(default, float):
                overrides[key] = float(value)
            elif isinstance(default, list):
                overrides[key] = json.loads(value) if value.strip().startswith("[") else [p.strip() for p in value.split(",") if p.strip()]
            else:
                overrides[key] = value
        except ValueError as exc:
            raise Auto3DError(f"bad value for {key}: {value!r} ({exc})") from exc
    return overrides


def settings_for(args: argparse.Namespace) -> Settings:
    set_verbose(bool(getattr(args, "verbose", False)))
    return load_settings(getattr(args, "config", None), overrides_from_args(args))


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> int:
    settings = settings_for(args)
    log(f"codex_auto3d {__version__} setup (project {PROJECT_ROOT}, skill {SKILL_ROOT})")
    ok = True

    npm = shutil.which("npm")
    if npm is None:
        log("npm not found — install Node.js 18+ (https://nodejs.org) then re-run setup", level="error")
        ok = False
    elif not args.skip_node:
        log("installing node runtime (three, esbuild, typescript) into node/ …")
        completed = subprocess.run([npm, "install", "--no-audit", "--no-fund"], cwd=str(NODE_DIR), text=True, capture_output=True)
        if completed.returncode != 0:
            log(f"npm install failed:\n{completed.stderr[-2000:]}", level="error")
            ok = False
        else:
            log("node runtime ready", level="ok")

    if not args.skip_browser:
        python = sys.executable
        if not VENV_DIR.exists():
            log(f"creating virtualenv {relpath(VENV_DIR)} for Playwright …")
            completed = subprocess.run([python, "-m", "venv", str(VENV_DIR)], text=True, capture_output=True)
            if completed.returncode != 0:
                log(f"venv creation failed: {completed.stderr[-1500:]}", level="error")
                ok = False
        venv_python = VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if venv_python.exists():
            log("installing Playwright + Chromium (first time downloads ~150 MB) …")
            steps = [
                [str(venv_python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
                [str(venv_python), "-m", "pip", "install", "--quiet", "playwright>=1.45"],
                [str(venv_python), "-m", "playwright", "install", "chromium"],
            ]
            for step in steps:
                completed = subprocess.run(step, text=True, capture_output=True)
                if completed.returncode != 0:
                    log(f"step failed: {' '.join(step[-3:])}\n{completed.stderr[-2000:]}", level="error")
                    ok = False
                    break
            else:
                log("Playwright + Chromium ready", level="ok")

    if args.link_skill:
        for target in (Path.home() / ".codex" / "skills" / "img2threejs", Path.home() / ".agents" / "skills" / "img2threejs"):
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink() or target.exists():
                    log(f"skill link exists: {target} → {target.resolve()}")
                    continue
                target.symlink_to(SKILL_ROOT, target_is_directory=True)
                log(f"linked {target} → {SKILL_ROOT}", level="ok")
            except OSError as exc:
                log(f"could not link {target}: {exc}", level="warn")

    if not CONFIG_FILE.exists():
        write_json(CONFIG_FILE, {"_comment": "codex_auto3d settings; every key falls back to DEFAULTS in auto3d/config.py", **{k: v for k, v in DEFAULTS.items()}})
        log(f"wrote default config {relpath(CONFIG_FILE)}", level="ok")
    (settings.work_root_path).mkdir(parents=True, exist_ok=True)
    log("setup finished" if ok else "setup finished with errors — see above", level="ok" if ok else "warn")
    return 0 if ok else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = settings_for(args)
    checks: list[tuple[str, bool | None, str]] = []

    checks.append(("python", sys.version_info >= (3, 10), sys.version.split()[0]))
    checks.append(("project root", (PROJECT_ROOT / "auto3d.py").is_file(), str(PROJECT_ROOT)))
    skill_ok = (SKILL_ROOT / "SKILL.md").is_file() and (SKILL_ROOT / "forge").is_dir()
    skill_note = str(SKILL_ROOT) if skill_ok else f"no img2threejs checkout at {SKILL_ROOT} — restore vendor/img2threejs or set IMG2THREEJS_ROOT"
    checks.append(("img2threejs skill", skill_ok, skill_note))

    version = codex_version(settings)
    checks.append(("codex cli", version is not None, version or f"'{settings.codex_bin}' not found — npm i -g @openai/codex"))
    if version is not None:
        logged_in, status_text = login_status(settings)
        checks.append(("codex login", logged_in, status_text.splitlines()[0] if status_text else ""))
        imagegen = codex_home() / "skills" / ".system" / "imagegen" / "SKILL.md"
        checks.append(("imagegen skill", imagegen.is_file() or None, str(imagegen) if imagegen.is_file() else "not materialised yet (run `codex exec \"hi\"` once, or it appears on first use)"))
    checks.append(("node runtime", three_installed() and esbuild_bin() is not None, f"three+esbuild in {relpath(NODE_DIR / 'node_modules')}" if three_installed() else "run setup"))
    checks.append(("typescript", tsc_bin() is not None or None, "tsc available" if tsc_bin() else "optional (typecheck evidence)"))
    python = playwright_python()
    browser_ok = False
    browser_note = "run setup (Playwright + Chromium)"
    if python is not None:
        probe = subprocess.run(
            [str(python), "-c", "from playwright.sync_api import sync_playwright\nimport sys\nwith sync_playwright() as p:\n    print(p.chromium.executable_path)"],
            text=True,
            capture_output=True,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            exe = Path(probe.stdout.strip())
            browser_ok = exe.exists()
            browser_note = f"{exe}" if browser_ok else f"chromium missing at {exe} — run `{python} -m playwright install chromium`"
        else:
            browser_note = probe.stderr.strip()[-300:] or "playwright import failed"
    checks.append(("playwright+chromium", browser_ok, browser_note))
    api_key = bool(os.environ.get("OPENAI_API_KEY"))
    checks.append(("OPENAI_API_KEY", api_key or None, "set (API image fallback available)" if api_key else "not set (only needed for --image-backend api)"))
    skill_links = [p for p in (Path.home() / ".codex" / "skills" / "img2threejs", Path.home() / ".agents" / "skills" / "img2threejs") if p.exists()]
    checks.append(("skill link", bool(skill_links) or None, ", ".join(str(p) for p in skill_links) if skill_links else "optional: setup --link-skill (the build prompt reads ./SKILL.md directly)"))
    forge_ok = subprocess.run([sys.executable, str(SKILL_ROOT / "forge" / "state.py"), "--help"], capture_output=True).returncode == 0
    checks.append(("forge scripts", forge_ok, f"{relpath(SKILL_ROOT / 'forge' / 'state.py')} runs"))
    checks.append(("config", CONFIG_FILE.is_file() or None, relpath(CONFIG_FILE) if CONFIG_FILE.is_file() else "using built-in defaults"))

    failed = 0
    for name, state, note in checks:
        mark = "OK " if state else ("-- " if state is None else "FAIL")
        if state is False:
            failed += 1
        print(f"[{mark}] {name:20s} {note}")
    print()
    print(f"target pass for quality={settings.quality}: {settings.target_pass} · image backend: {settings.image_backend} · sandbox: {settings.sandbox}")
    if failed:
        print(f"{failed} check(s) failed — fix them before `run`.")
    return 1 if failed else 0


def _run_one(
    settings: Settings,
    concept: str,
    name: str | None,
    stage_until: str | None,
    *,
    reference_inputs: dict[str, str] | None = None,
    reference_camera: dict[str, float] | None = None,
) -> tuple[str, str]:
    extra: dict[str, object] = {}
    if reference_inputs:
        extra["referenceInputs"] = reference_inputs
        if reference_camera:
            extra["referenceCamera"] = reference_camera
    job = create_job(settings, concept, name=name, extra=extra or None)
    set_log_file(job.path("auto3d.log"))
    if reference_inputs:
        supplied = ", ".join(f"{view}={Path(path).name}" for view, path in reference_inputs.items())
        log(f"job {job.id} created from supplied reference: {supplied}")
    else:
        log(f"job {job.id} created for concept: {concept}")
    pipeline = Pipeline(settings, job)
    status = pipeline.run(until=stage_until)
    print("\n" + describe_job(job) + "\n")
    return status, str(job.dir)


def _reference_inputs(args: argparse.Namespace) -> tuple[dict[str, str], dict[str, float] | None]:
    """Parse --reference / --view NAME=PATH / --reference-camera into job state. The hero comes
    first so the intake turn sees it as image 1."""
    if not getattr(args, "reference", None):
        if getattr(args, "view", None):
            raise Auto3DError("--view needs --reference (the hero image those views belong to)")
        if getattr(args, "reference_camera", None):
            raise Auto3DError("--reference-camera only applies together with --reference")
        return {}, None
    hero = Path(args.reference).expanduser().resolve()
    if not hero.is_file():
        raise Auto3DError(f"--reference file not found: {hero}")
    inputs = {"hero": str(hero)}
    for item in getattr(args, "view", None) or []:
        view, _, raw = str(item).partition("=")
        view = view.strip()
        if not raw.strip():
            raise Auto3DError(f"--view expects NAME=PATH, got {item!r}")
        if view == "hero" or view not in VIEW_CAMERAS:
            choices = ", ".join(name for name in VIEW_CAMERAS if name != "hero")
            raise Auto3DError(f"unknown view {view!r}; choose from {choices}")
        path = Path(raw.strip()).expanduser().resolve()
        if not path.is_file():
            raise Auto3DError(f"--view {view} file not found: {path}")
        inputs[view] = str(path)
    camera = None
    if getattr(args, "reference_camera", None):
        parts = [part.strip() for part in str(args.reference_camera).replace("/", ",").split(",") if part.strip()]
        if len(parts) != 2:
            raise Auto3DError("--reference-camera expects AZ,EL in degrees, e.g. --reference-camera 35,0")
        try:
            camera = {"azimuth": float(parts[0]), "elevation": float(parts[1])}
        except ValueError as exc:
            raise Auto3DError(f"--reference-camera is not a pair of numbers: {args.reference_camera!r}") from exc
    return inputs, camera


def cmd_run(args: argparse.Namespace) -> int:
    settings = settings_for(args)
    reference_inputs, reference_camera = _reference_inputs(args)
    concept = args.prompt or (args.prompt_file.read_text(encoding="utf-8").strip() if args.prompt_file else "")
    if not concept and not reference_inputs:
        raise Auto3DError("provide --prompt \"...\", --prompt-file, or --reference IMAGE")
    if reference_inputs and settings.views:
        log("--views asks the pipeline to generate extra views; with --reference use --view NAME=PATH instead", level="warn")
    status, _ = _run_one(
        settings,
        concept,
        args.name,
        args.until,
        reference_inputs=reference_inputs,
        reference_camera=reference_camera,
    )
    return 0 if status in {"completed", "partial"} else 1


def cmd_batch(args: argparse.Namespace) -> int:
    base_settings = settings_for(args)
    items = parse_batch_file(args.file)
    log(f"batch: {len(items)} concept(s) from {relpath(args.file)}")
    results = []
    started = time.monotonic()
    for index, item in enumerate(items, start=1):
        overrides = dict(overrides_from_args(args))
        overrides.update(item.overrides)
        try:
            settings = load_settings(getattr(args, "config", None), overrides)
        except Auto3DError as exc:
            log(f"[{index}/{len(items)}] invalid overrides for row {item.source_row}: {exc}", level="error")
            results.append({"row": item.source_row, "concept": item.concept, "status": "invalid", "error": str(exc)})
            if not args.continue_on_error:
                break
            continue
        log(f"[{index}/{len(items)}] {item.concept}")
        try:
            status, job_dir = _run_one(settings, item.concept, item.name, args.until)
            results.append({"row": item.source_row, "concept": item.concept, "status": status, "job": relpath(Path(job_dir))})
        except Auto3DError as exc:
            log(f"[{index}/{len(items)}] failed: {exc}", level="error")
            results.append({"row": item.source_row, "concept": item.concept, "status": "failed", "error": str(exc)})
            if not args.continue_on_error:
                break
        except KeyboardInterrupt:
            log("batch interrupted", level="warn")
            break
    summary_path = base_settings.work_root_path / f"batch-{time.strftime('%Y%m%d-%H%M%S')}.json"
    write_json(summary_path, {"file": str(args.file), "elapsedSec": round(time.monotonic() - started), "results": results})
    completed = sum(1 for r in results if r["status"] == "completed")
    print(f"\nbatch done: {completed}/{len(items)} completed · {human_duration(time.monotonic() - started)} · summary {relpath(summary_path)}")
    for result in results:
        print(f"  [{result['status']:9s}] {result['concept'][:60]}  {result.get('job') or result.get('error', '')}")
    return 0 if completed == len(items) else 1


def cmd_resume(args: argparse.Namespace) -> int:
    settings = settings_for(args)
    job = load_job(args.job)
    # the job snapshot wins for stable keys unless the user overrides on the CLI
    snapshot = job.state.get("settings") or {}
    for key, value in snapshot.items():
        if key in DEFAULTS and key not in overrides_from_args(args):
            settings.set(key, value)
    set_log_file(job.path("auto3d.log"))
    log(f"resuming job {job.id} (status {job.state.get('status')})")
    if args.restart_stage:
        job.stage(args.restart_stage)["status"] = "pending"
        if args.restart_stage == "build":
            job.stage("build").pop("threadId", None)
            job.stage("build")["turns"] = []
        job.save()
    status = Pipeline(settings, job).run(until=args.until)
    print("\n" + describe_job(job) + "\n")
    return 0 if status in {"completed", "partial"} else 1


def cmd_preview(args: argparse.Namespace) -> int:
    settings = settings_for(args)
    if args.job:
        job = load_job(args.job)
        build = job.stage("build")
        factory = Path(args.factory) if args.factory else None
        if factory is None:
            candidates = sorted(job.path("src").glob("*.ts"), key=lambda p: p.stat().st_mtime, reverse=True) if job.path("src").is_dir() else []
            if not candidates:
                raise Auto3DError(f"no factory .ts under {relpath(job.path('src'))}")
            factory = candidates[0]
        reference = Path(args.reference) if args.reference else (REPO_ROOT / job.stage("image").get("hero")) if job.stage("image").get("hero") else None
        spec = job.path("object-sculpt-spec.json")
        out_dir = Path(args.out_dir) if args.out_dir else job.path("preview")
        hero = {"azimuth": float(settings.hero_azimuth), "elevation": float(settings.hero_elevation)}
        prompt_json = job.path("prompt", "prompt.json")
        if prompt_json.is_file():
            camera = (json.loads(prompt_json.read_text(encoding="utf-8")).get("camera") or {})
            if "azimuth" in camera and "elevation" in camera:
                hero = {"azimuth": float(camera["azimuth"]), "elevation": float(camera["elevation"])}
        if args.azimuth is not None:
            hero["azimuth"] = float(args.azimuth)
        if args.elevation is not None:
            hero["elevation"] = float(args.elevation)
        character = (build.get("profile") or job.state.get("profile")) == "character"
        title = job.state.get("subject")
    else:
        if not args.factory:
            raise Auto3DError("provide --job <dir> or --factory <file.ts>")
        factory = Path(args.factory)
        reference = Path(args.reference) if args.reference else None
        spec = Path(args.spec) if args.spec else None
        out_dir = Path(args.out_dir) if args.out_dir else factory.parent / "preview"
        hero = {"azimuth": float(args.azimuth if args.azimuth is not None else settings.hero_azimuth), "elevation": float(args.elevation if args.elevation is not None else settings.hero_elevation)}
        character = args.character
        title = None
    summary = render_factory(
        factory=factory.resolve(),
        out_dir=out_dir.resolve(),
        settings=settings,
        reference=reference.resolve() if reference else None,
        spec=spec.resolve() if spec and spec.is_file() else None,
        pass_id=args.pass_id,
        hero=hero,
        character=character,
        title=title,
        history_tag=args.tag,
    )
    print(json.dumps({key: summary[key] for key in ("previewHtml", "comparisonSheet", "captures", "meshes", "triangles", "consoleErrors", "gatesSummary")}, indent=2, ensure_ascii=False))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from auto3d.report import write_gallery, write_job_report

    settings = settings_for(args)
    job = load_job(args.job)
    path = write_job_report(job, settings)
    gallery = write_gallery(settings)
    print(relpath(path))
    if gallery:
        print(relpath(gallery))
    return 0


def cmd_gallery(args: argparse.Namespace) -> int:
    from auto3d.report import write_gallery

    settings = settings_for(args)
    path = write_gallery(settings)
    print(relpath(path) if path else "no jobs yet")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    settings = settings_for(args)
    jobs = list_jobs(settings.work_root_path)
    if not jobs:
        print("no jobs yet")
        return 0
    for job in jobs:
        progress = job.stage("build").get("progress") or {}
        print(f"{job.id:48s} {str(job.state.get('status')):10s} passes={','.join(progress.get('completedPasses') or []) or '-'} fidelity={progress.get('latestFidelity')}")
    return 0


def cmd_prompt(args: argparse.Namespace) -> int:
    """Author the prompt only (useful to inspect/edit it before spending image credits)."""
    settings = settings_for(args)
    status, _ = _run_one(settings, args.prompt, args.name, "prompt")
    return 0 if status else 1


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auto3d", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"codex_auto3d {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    setup = commands.add_parser("setup", help="install the node runtime and Playwright, write a default config")
    add_common(setup)
    setup.add_argument("--link-skill", action="store_true", help="symlink this checkout into ~/.codex/skills and ~/.agents/skills")
    setup.add_argument("--skip-node", action="store_true")
    setup.add_argument("--skip-browser", action="store_true")
    setup.set_defaults(func=cmd_setup)

    doctor = commands.add_parser("doctor", help="check codex, login, toolchain and skill wiring")
    add_common(doctor)
    doctor.set_defaults(func=cmd_doctor)

    run = commands.add_parser("run", help="one concept → image → 3D model")
    add_common(run)
    add_run_overrides(run)
    run.add_argument("--prompt", "-p", help="concept text (Korean or English)")
    run.add_argument("--prompt-file", type=Path, help="read the concept from a text file")
    run.add_argument("--reference", type=Path, help="build from an image you already have instead of generating one (PNG/JPEG)")
    run.add_argument(
        "--view",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="extra supplied reference view, repeatable: --view front=front.png --view side=side.png (front|side|back|top)",
    )
    run.add_argument("--reference-camera", dest="reference_camera", metavar="AZ,EL", help="camera of --reference in degrees, e.g. 35,0 (default: the intake turn estimates it)")
    run.add_argument("--name", help="job name / directory slug")
    run.add_argument("--until", choices=["prompt", "image", "build", "report"], help="stop after this stage (e.g. --until image)")
    run.set_defaults(func=cmd_run)

    batch = commands.add_parser("batch", help="many concepts from .txt/.csv/.json/.xlsx, sequentially")
    add_common(batch)
    add_run_overrides(batch)
    batch.add_argument("--file", "-f", type=Path, required=True)
    batch.add_argument("--continue-on-error", action="store_true", default=True)
    batch.add_argument("--stop-on-error", dest="continue_on_error", action="store_false")
    batch.add_argument("--until", choices=["prompt", "image", "build", "report"])
    batch.set_defaults(func=cmd_batch)

    resume = commands.add_parser("resume", help="continue an interrupted or partial job")
    add_common(resume)
    add_run_overrides(resume)
    resume.add_argument("--job", required=True, type=Path)
    resume.add_argument("--restart-stage", choices=["prompt", "image", "build", "report"], help="redo this stage from scratch")
    resume.add_argument("--until", choices=["prompt", "image", "build", "report"])
    resume.set_defaults(func=cmd_resume)

    preview = commands.add_parser("preview", help="bundle + render + capture + gates for a factory (no Codex)")
    add_common(preview)
    preview.add_argument("--job", type=Path)
    preview.add_argument("--factory", type=Path)
    preview.add_argument("--reference", type=Path)
    preview.add_argument("--spec", type=Path)
    preview.add_argument("--out-dir", type=Path)
    preview.add_argument("--pass-id", choices=PASS_ORDER)
    preview.add_argument("--azimuth", type=float)
    preview.add_argument("--elevation", type=float)
    preview.add_argument("--character", action="store_true")
    preview.add_argument("--tag", help="history tag, e.g. turn-03")
    preview.set_defaults(func=cmd_preview)

    report = commands.add_parser("report", help="(re)generate report.html for a job and refresh the gallery")
    add_common(report)
    report.add_argument("--job", required=True, type=Path)
    report.set_defaults(func=cmd_report)

    gallery = commands.add_parser("gallery", help="rebuild work/auto3d/index.html")
    add_common(gallery)
    gallery.set_defaults(func=cmd_gallery)

    listing = commands.add_parser("list", help="list jobs")
    add_common(listing)
    listing.set_defaults(func=cmd_list)

    prompt = commands.add_parser("prompt", help="author the image prompt only")
    add_common(prompt)
    add_run_overrides(prompt)
    prompt.add_argument("--prompt", "-p", required=True)
    prompt.add_argument("--name")
    prompt.set_defaults(func=cmd_prompt)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except Auto3DError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
