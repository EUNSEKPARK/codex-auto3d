"""Job orchestration: prompt → image → build loop → report.

The build loop follows the repository's own division of labour — Python runs the deterministic
work (bundle, render, capture, gates, evidence packaging) and Codex spends tokens only on judgment
and code. Each Codex turn ends in a structured JSON message; the orchestrator verifies claims
against the files on disk (factory hash, spec reviewHistory) before trusting them.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import imagegen, prompts, schemas
from .codex import CodexResult, run_codex
from .config import PASS_ORDER, Settings
from .jobs import Job
from .preview import render_factory
from .util import (
    Auto3DError,
    REPO_ROOT,
    Stopwatch,
    human_duration,
    log,
    now_iso,
    pascal_case,
    read_json,
    relpath,
    set_log_file,
    sha256_file,
    slugify,
    write_json,
    write_text,
)

STAGES = ("prompt", "image", "build", "report")
NEAR_IDENTICAL_BITS = 2


def hamming(a: int, b: int) -> int:
    return bin(int(a) ^ int(b)).count("1")


@dataclass
class TurnResult:
    stage: str  # factory-ready | done | blocked | failed | invalid
    pass_id: str | None
    factory_path: str | None
    spec_path: str | None
    factory_function: str | None
    review: dict[str, Any] | None
    state_status: str | None
    corrections_used: int | None
    message: str
    message_ko: str
    raw: CodexResult

    @classmethod
    def from_codex(cls, result: CodexResult) -> "TurnResult":
        data = result.structured if isinstance(result.structured, dict) else {}
        stage = str(data.get("stage") or "invalid")
        if stage not in {"factory-ready", "done", "blocked", "failed"}:
            stage = "invalid"
        review = data.get("review") if isinstance(data.get("review"), dict) else None
        return cls(
            stage=stage,
            pass_id=data.get("pass_id") if data.get("pass_id") in PASS_ORDER else None,
            factory_path=data.get("factory_path"),
            spec_path=data.get("spec_path"),
            factory_function=data.get("factory_function"),
            review=review,
            state_status=data.get("state_status"),
            corrections_used=data.get("corrections_used") if isinstance(data.get("corrections_used"), int) else None,
            message=str(data.get("message") or ""),
            message_ko=str(data.get("message_ko") or ""),
            raw=result,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "passId": self.pass_id,
            "factoryPath": self.factory_path,
            "specPath": self.spec_path,
            "factoryFunction": self.factory_function,
            "review": self.review,
            "stateStatus": self.state_status,
            "correctionsUsed": self.corrections_used,
            "message": self.message,
            "messageKo": self.message_ko,
            "threadId": self.raw.thread_id,
            "usage": self.raw.usage,
            "durationSec": round(self.raw.duration, 1),
            "timedOut": self.raw.timed_out,
            "returncode": self.raw.returncode,
            "errors": self.raw.errors[-5:],
        }


class Pipeline:
    def __init__(self, settings: Settings, job: Job) -> None:
        self.settings = settings
        self.job = job
        self.clock = Stopwatch()

    @property
    def codex_dir(self) -> Path:
        path = self.job.path("codex")
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------ helpers
    @property
    def prompt_data(self) -> dict[str, Any]:
        path = self.job.path("prompt", "prompt.json")
        if not path.is_file():
            raise Auto3DError("prompt stage has not produced prompt/prompt.json yet")
        return read_json(path)

    @property
    def hero_camera(self) -> dict[str, float]:
        """The camera the reference was generated with. Once prompt.json exists it is the
        authority (a config change after the fact must not move the hero render away from the
        reference framing); before that, the settings decide."""
        path = self.job.path("prompt", "prompt.json")
        if path.is_file():
            try:
                camera = read_json(path).get("camera") or {}
                if isinstance(camera, dict) and "azimuth" in camera and "elevation" in camera:
                    return {"azimuth": float(camera["azimuth"]), "elevation": float(camera["elevation"])}
            except (ValueError, TypeError):
                pass
        pinned = self.job.state.get("referenceCamera")
        if isinstance(pinned, dict) and "azimuth" in pinned and "elevation" in pinned:
            # a supplied reference was shot by someone else; the operator's measurement wins over
            # the settings default, which only describes images this pipeline generates itself
            return {"azimuth": float(pinned["azimuth"]), "elevation": float(pinned["elevation"])}
        return {"azimuth": float(self.settings.hero_azimuth), "elevation": float(self.settings.hero_elevation)}

    @property
    def reference_inputs(self) -> dict[str, str]:
        """Supplied reference files, {"hero": path, "front": path, …}, empty in concept mode."""
        value = self.job.state.get("referenceInputs")
        return dict(value) if isinstance(value, dict) else {}

    @property
    def reference_mode(self) -> bool:
        return bool(self.reference_inputs.get("hero"))

    def _time_left(self) -> float:
        return self.settings.job_timeout_min * 60 - self.clock.elapsed()

    def _turn_timeout(self) -> float:
        return max(120.0, min(self.settings.turn_timeout_min * 60, self._time_left()))

    def _record_usage(self, result: CodexResult) -> None:
        self.job.add_usage(result.usage)

    # ------------------------------------------------------------------ run
    def run(self, *, only: str | None = None, until: str | None = None) -> str:
        """Run pending stages in order. Returns the final job status."""
        self.job.set_status("running")
        self.job.state.setdefault("startedAt", now_iso())
        try:
            for stage in STAGES:
                if only and stage != only:
                    continue
                record = self.job.stage(stage)
                if record.get("status") == "done" and stage != "report":
                    log(f"stage {stage}: already done, skipping")
                    continue
                handler = getattr(self, f"stage_{stage}")
                handler()
                if until and stage == until:
                    break
        except Auto3DError as exc:
            self.job.add_error(str(exc))
            self.job.set_status("failed")
            log(f"job {self.job.id} failed: {exc}", level="error")
            try:
                self.stage_report()
            except Exception as report_exc:  # noqa: BLE001
                log(f"report generation failed too: {report_exc}", level="warn")
            raise
        except KeyboardInterrupt:
            self.job.add_error("interrupted by user")
            self.job.set_status("interrupted")
            raise
        status = self.job.state.get("status", "unknown")
        return status

    # ------------------------------------------------------------------ stage: prompt
    def stage_prompt(self) -> None:
        record = self.job.stage("prompt")
        record.update({"status": "running", "startedAt": now_iso()})
        self.job.save()
        concept = self.job.state["concept"]
        supplied = self.reference_inputs
        reference_mode = self.reference_mode
        supplied_views = [name for name in supplied if name != "hero"]
        views = supplied_views if reference_mode else list(self.settings.views)
        data: dict[str, Any] | None = None
        images: list[Path] = []
        if self.settings.prompt_author == "codex" or reference_mode:
            schema_path = schemas.write_schema(schemas.PROMPT_SCHEMA, self.codex_dir / "schemas" / "prompt.schema.json")
            if reference_mode:
                # nothing to generate: the turn reads the supplied images and reports what they show
                log(f"stage prompt: asking Codex to read the supplied reference ({len(supplied)} image(s))")
                images = [Path(supplied["hero"])] + [Path(supplied[name]) for name in supplied_views]
                text = prompts.reference_intake(
                    concept,
                    hero_camera=self.hero_camera,
                    camera_pinned=isinstance(self.job.state.get("referenceCamera"), dict),
                    supplied_views=supplied_views,
                    profile_hint=self.settings.profile,
                    complexity_hint=self.settings.complexity,
                )
            else:
                log("stage prompt: asking Codex to write the 3D-friendly image prompt")
                text = prompts.prompt_author(
                    concept,
                    style=self.settings.style,
                    profile_hint=self.settings.profile,
                    complexity_hint=self.settings.complexity,
                    hero_camera=self.hero_camera,
                    views=views,
                )
            result = run_codex(
                self.settings,
                text,
                label="prompt",
                events_path=self.codex_dir / "prompt.events.jsonl",
                last_message_path=self.codex_dir / "prompt.last.json",
                prompt_path=self.codex_dir / "prompt.prompt.md",
                images=images or None,
                output_schema=schema_path,
                sandbox="read-only",
                network=False,
                cwd=REPO_ROOT,
                ephemeral=True,
                timeout_s=min(900, self._turn_timeout()),
            )
            self._record_usage(result)
            candidate = result.structured if isinstance(result.structured, dict) else None
            problems = schemas.validate_against(schemas.PROMPT_SCHEMA, candidate) if candidate else ["no JSON"]
            if candidate and not problems:
                data = candidate
            elif candidate and candidate.get("image_prompt"):
                log(f"prompt JSON has schema issues ({problems[:3]}); using it anyway", level="warn")
                data = _fill_prompt_defaults(candidate, views)
            else:
                fallback = "a minimal stub" if reference_mode else "the template author"
                log(f"Codex did not return a usable prompt; falling back to {fallback}", level="warn")
                record.setdefault("warnings", []).append(f"codex prompt author failed: {problems[:3]} / {result.errors[-1:]}")
        if data is None:
            if reference_mode:
                data = _stub_reference_prompt(concept, self.job.state.get("name"), views)
                data["author"] = "stub"
            else:
                data = prompts.template_prompt(concept, style=self.settings.style, hero_camera=self.hero_camera, views=views)
                data["subject_slug"] = slugify(data["subject_name"])
                data["author"] = "template"
        else:
            data["author"] = "codex"
        # the orchestrator owns the camera numbers (they drive the hero capture). For a supplied
        # reference nobody here chose the camera, so an operator measurement wins, and otherwise the
        # intake turn's estimate does — the settings default only describes images we generate.
        if reference_mode and not isinstance(self.job.state.get("referenceCamera"), dict):
            estimate = data.get("camera") if isinstance(data.get("camera"), dict) else {}
            try:
                camera = {"azimuth": float(estimate["azimuth"]), "elevation": float(estimate["elevation"])}
            except (KeyError, TypeError, ValueError):
                camera = dict(self.hero_camera)
                log("stage prompt: no camera estimate for the reference; using the default hero framing", level="warn")
            else:
                log(f"stage prompt: hero camera read off the reference — azimuth {camera['azimuth']:.0f}°, elevation {camera['elevation']:.0f}°")
            self.job.state["referenceCamera"] = camera
            data["camera"] = camera
        else:
            data["camera"] = dict(self.hero_camera)
        if self.settings.profile != "auto":
            data["profile"] = self.settings.profile
        if self.settings.complexity != "auto":
            data["complexity"] = self.settings.complexity
        if not data.get("subject_slug"):
            data["subject_slug"] = slugify(data.get("subject_name") or concept)
        data["views_requested"] = views
        write_json(self.job.path("prompt", "prompt.json"), data)
        write_text(self.job.path("prompt", "image_prompt.txt"), data["image_prompt"].strip() + "\n")
        for view in views:
            view_prompt = (data.get("view_prompts") or {}).get(view)
            if view_prompt:
                write_text(self.job.path("prompt", f"view_{view}.txt"), str(view_prompt).strip() + "\n")
        self.job.state["subject"] = data.get("subject_name")
        self.job.state["profile"] = data.get("profile")
        self.job.state["complexity"] = data.get("complexity")
        record.update({"status": "done", "finishedAt": now_iso(), "path": relpath(self.job.path("prompt", "prompt.json")), "author": data["author"]})
        self.job.save()
        self._maybe_rename_job(data)
        log(f"stage prompt: done — subject '{data.get('subject_name')}', profile {data.get('profile')}, complexity {data.get('complexity')}")

    def _maybe_rename_job(self, data: dict[str, Any]) -> None:
        """Jobs created from a non-ASCII concept get a generic slug; rename them once the prompt
        author supplies an English subject slug. Nothing else references the directory yet."""
        current = self.job.dir.name
        if not current.endswith("-subject"):
            return
        slug = slugify(str(data.get("subject_slug") or data.get("subject_name") or ""), fallback="")
        if not slug:
            return
        target = self.job.dir.with_name(current[: -len("subject")] + slug)
        if target.exists():
            return
        self.job.dir.rename(target)
        self.job.dir = target
        self.job.state["id"] = target.name
        self.job.save()
        set_log_file(target / "auto3d.log")
        log(f"job renamed → {target.name}")

    # ------------------------------------------------------------------ stage: image
    def stage_image(self) -> None:
        record = self.job.stage("image")
        record.update({"status": "running", "startedAt": now_iso(), "attempts": record.get("attempts", [])})
        self.job.save()
        if self.reference_mode:
            self._adopt_reference(record)
            return
        data = self.prompt_data
        hero_path = self.job.path("reference", "hero.png")
        hero_path.parent.mkdir(parents=True, exist_ok=True)
        image_prompt = str(data["image_prompt"])
        attempts = int(self.settings.image_attempts)
        retry_note: str | None = None
        hero_record: imagegen.ImageRecord | None = None
        verdict: dict[str, Any] = {}
        if record.get("heroAdmitted") and hero_path.is_file() and self.job.path("reference", "hero.json").is_file():
            # resumed after the hero was already accepted: keep it, only the views may be missing
            log("stage image: hero reference already admitted, reusing it")
            saved = read_json(self.job.path("reference", "hero.json"))
            hero_record = imagegen.ImageRecord(
                path=hero_path,
                backend=str(saved.get("backend") or "codex"),
                model=saved.get("model"),
                size=saved.get("size"),
                prompt_used=str(saved.get("promptUsed") or image_prompt),
                thread_id=saved.get("threadId"),
                notes=list(saved.get("notes") or []),
            )
            verdict = {"admitted": True, "provenance": {"pHash": record.get("heroHash")}}
            attempts = 0
        for attempt in range(1, attempts + 1):
            log(f"stage image: generating hero reference (attempt {attempt}/{attempts}, backend {self.settings.image_backend})")
            hero_record = imagegen.generate_reference(
                self.settings,
                image_prompt,
                hero_path,
                codex_dir=self.codex_dir,
                label=f"image-hero-{attempt}",
                retry_note=retry_note,
            )
            self.job.add_usage(hero_record.usage)
            probe = imagegen.probe_image(hero_path)
            verdict = imagegen.check_admission(hero_path)
            entry = {
                "attempt": attempt,
                "backend": hero_record.backend,
                "model": hero_record.model,
                "probe": {key: probe.get(key) for key in ("type", "width", "height", "bytes", "technicalSuitability", "warnings")},
                "admission": verdict,
                "threadId": hero_record.thread_id,
                "notes": hero_record.notes,
            }
            record["attempts"].append(entry)
            self.job.save()
            if verdict.get("admitted"):
                log(f"stage image: hero admitted ({probe.get('width')}x{probe.get('height')}, coverage {verdict.get('provenance', {}).get('foregroundCoverage')})", level="ok")
                break
            reasons = verdict.get("reasons") or []
            log(f"stage image: reference rejected by the admission gate: {reasons}", level="warn")
            rejected = self.job.path("reference", f"rejected-{attempt}.png")
            shutil.copyfile(hero_path, rejected)
            entry["rejectedCopy"] = relpath(rejected)
            retry_note = imagegen.admission_feedback(verdict)
        if hero_record is None:
            raise Auto3DError("no image was generated")
        if not verdict.get("admitted"):
            log("stage image: continuing with the last image despite admission warnings (the build turn re-checks it)", level="warn")
        imagegen.save_record(hero_record, self.job.path("reference", "hero.json"))
        record["hero"] = relpath(hero_path)
        record["heroAdmitted"] = bool(verdict.get("admitted"))
        record["heroHash"] = verdict.get("provenance", {}).get("pHash")
        record["backend"] = hero_record.backend
        record["threadId"] = hero_record.thread_id
        self.job.state["artifacts"]["hero"] = relpath(hero_path)

        # extra views ------------------------------------------------------
        # The duplicate gate (pHash within 6 bits) exists to catch *accidental* duplicates. Extra
        # views are the same subject on the same backdrop by design, so a symmetric object's front
        # view can legitimately sit within that distance of the hero; only a near-identical image
        # (<= NEAR_IDENTICAL_BITS) is refused, closer matches are recorded as a warning.
        views: dict[str, str] = {}
        admitted_hashes: list[int] = [value for value in [record.get("heroHash")] if isinstance(value, int)]
        for view in self.settings.views:
            view_prompt = (data.get("view_prompts") or {}).get(view)
            if not view_prompt:
                log(f"stage image: no prompt for view '{view}', skipping", level="warn")
                continue
            out = self.job.path("reference", f"{view}.png")
            try:
                view_record = imagegen.generate_reference(
                    self.settings,
                    str(view_prompt),
                    out,
                    codex_dir=self.codex_dir,
                    label=f"image-{view}",
                    resume_thread=record.get("threadId") if hero_record.backend == "codex" else None,
                    view=view,
                    reference_images=[hero_path],
                )
            except Auto3DError as exc:
                log(f"stage image: view '{view}' failed: {exc}", level="warn")
                record.setdefault("viewErrors", {})[view] = str(exc)
                continue
            self.job.add_usage(view_record.usage)
            view_verdict = imagegen.check_admission(out, viewpoint=view)
            imagegen.save_record(view_record, self.job.path("reference", f"{view}.json"))
            view_hash = view_verdict.get("provenance", {}).get("pHash")
            distance = min((hamming(view_hash, other) for other in admitted_hashes), default=None) if isinstance(view_hash, int) else None
            view_verdict["hammingToAdmitted"] = distance
            if view_verdict.get("admitted") and distance is not None and distance <= NEAR_IDENTICAL_BITS:
                view_verdict["admitted"] = False
                view_verdict.setdefault("reasons", []).append(f"near-identical to an admitted reference (pHash distance {distance})")
            record.setdefault("viewAdmission", {})[view] = view_verdict
            if view_verdict.get("admitted"):
                views[view] = relpath(out)
                if isinstance(view_hash, int):
                    admitted_hashes.append(view_hash)
                note = f" (pHash distance {distance}, close to the hero — check it really shows a new angle)" if distance is not None and distance <= 6 else ""
                log(f"stage image: view '{view}' admitted{note}", level="ok" if not note else "warn")
            else:
                log(f"stage image: view '{view}' rejected ({view_verdict.get('reasons')}); it will not be used", level="warn")
        record["views"] = views
        record.update({"status": "done", "finishedAt": now_iso()})
        self.job.save()

    def _adopt_reference(self, record: dict[str, Any]) -> None:
        """`--reference`: the images already exist, so nothing is generated. Each supplied file is
        copied into the job and put through the same admission gate, and the stage record ends up
        in the same shape the generated path produces — from here on the build stage cannot tell
        the two apart. A rejected hero is a warning, not a failure: it is the operator's image and
        there is nothing to retry, so the reasons are recorded and the build turn sees them too."""
        supplied = self.reference_inputs
        hero_source = Path(supplied["hero"])
        hero_path = self._copy_reference(hero_source, "hero")
        probe = imagegen.probe_image(hero_path)
        verdict = imagegen.check_admission(hero_path)
        record["attempts"].append(
            {
                "attempt": 1,
                "backend": "supplied",
                "source": str(hero_source),
                "probe": {key: probe.get(key) for key in ("type", "width", "height", "bytes", "technicalSuitability", "warnings")},
                "admission": verdict,
            }
        )
        provenance = verdict.get("provenance") or {}
        if verdict.get("admitted"):
            log(
                f"stage image: supplied hero admitted ({probe.get('width')}x{probe.get('height')}, "
                f"coverage {provenance.get('foregroundCoverage')})",
                level="ok",
            )
        else:
            reasons = verdict.get("reasons") or []
            log(f"stage image: supplied hero fails the admission gate ({reasons}); continuing with it anyway", level="warn")
            record.setdefault("warnings", []).append(f"hero admission: {reasons}")
        imagegen.save_record(
            imagegen.ImageRecord(
                path=hero_path,
                backend="supplied",
                model=None,
                size=f"{probe.get('width')}x{probe.get('height')}",
                prompt_used=str(self.prompt_data.get("image_prompt") or "").strip(),
                notes=[f"supplied by the operator: {hero_source}"],
            ),
            self.job.path("reference", "hero.json"),
        )
        record["hero"] = relpath(hero_path)
        record["heroAdmitted"] = bool(verdict.get("admitted"))
        record["heroHash"] = provenance.get("pHash")
        record["backend"] = "supplied"
        self.job.state["artifacts"]["hero"] = relpath(hero_path)

        views: dict[str, str] = {}
        admitted_hashes: list[int] = [value for value in [record.get("heroHash")] if isinstance(value, int)]
        for view in [name for name in supplied if name != "hero"]:
            source = Path(supplied[view])
            out = self._copy_reference(source, view)
            view_verdict = imagegen.check_admission(out, viewpoint=view)
            view_hash = (view_verdict.get("provenance") or {}).get("pHash")
            distance = min((hamming(view_hash, other) for other in admitted_hashes), default=None) if isinstance(view_hash, int) else None
            view_verdict["hammingToAdmitted"] = distance
            if view_verdict.get("admitted") and distance is not None and distance <= NEAR_IDENTICAL_BITS:
                view_verdict["admitted"] = False
                view_verdict.setdefault("reasons", []).append(f"near-identical to an admitted reference (pHash distance {distance})")
            record.setdefault("viewAdmission", {})[view] = view_verdict
            imagegen.save_record(
                imagegen.ImageRecord(
                    path=out,
                    backend="supplied",
                    model=None,
                    size=None,
                    prompt_used="",
                    notes=[f"supplied by the operator: {source}"],
                ),
                self.job.path("reference", f"{view}.json"),
            )
            if view_verdict.get("admitted"):
                views[view] = relpath(out)
                if isinstance(view_hash, int):
                    admitted_hashes.append(view_hash)
                log(f"stage image: supplied view '{view}' admitted", level="ok")
            else:
                log(f"stage image: supplied view '{view}' rejected ({view_verdict.get('reasons')}); it will not be used", level="warn")
        record["views"] = views
        record.update({"status": "done", "finishedAt": now_iso()})
        self.job.save()

    def _copy_reference(self, source: Path, name: str) -> Path:
        """Copy a supplied image into the job's reference/ directory, keeping its real format.
        Only PNG and JPEG are accepted — the forge intake scripts read those two."""
        if not source.is_file():
            # a resume after the operator moved or deleted the original: the job's own copy stands in
            for existing in (self.job.path("reference", f"{name}.png"), self.job.path("reference", f"{name}.jpg")):
                if existing.is_file():
                    log(f"stage image: {source} is gone; reusing the copy already in the job ({relpath(existing)})", level="warn")
                    return existing
            raise Auto3DError(f"reference image not found: {source}")
        if imagegen.is_png(source):
            suffix = ".png"
        elif imagegen.is_jpeg(source):
            suffix = ".jpg"
        else:
            raise Auto3DError(f"reference must be a PNG or JPEG file: {source}")
        target = self.job.path("reference", f"{name}{suffix}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return target

    # ------------------------------------------------------------------ stage: build
    def stage_build(self) -> None:
        record = self.job.stage("build")
        record.setdefault("turns", [])
        record.update({"status": "running", "startedAt": record.get("startedAt") or now_iso()})
        data = self.prompt_data
        image_record = self.job.stage("image")
        hero_rel = image_record.get("hero") or relpath(self.job.path("reference", "hero.png"))
        hero_abs = REPO_ROOT / hero_rel
        if not hero_abs.is_file():
            raise Auto3DError(f"hero reference missing: {hero_rel}")
        views_rel: dict[str, str] = dict(image_record.get("views") or {})
        profile = data.get("profile") if data.get("profile") in {"generic", "character"} else "generic"
        complexity = data.get("complexity") if data.get("complexity") in {"simple", "moderate", "complex", "ultra-complex"} else "moderate"
        subject_name = str(data.get("subject_name") or self.job.state["concept"])[:80]
        type_name = pascal_case(subject_name)
        factory_rel = f"{self.job.rel}/src/create{type_name}Model.ts"
        spec_rel = f"{self.job.rel}/object-sculpt-spec.json"
        target_pass = self.settings.target_pass
        record.update(
            {
                "targetPass": target_pass,
                "profile": profile,
                "complexity": complexity,
                "factoryPath": factory_rel,
                "specPath": spec_rel,
                "subjectName": subject_name,
            }
        )
        self.job.save()
        schema_path = schemas.write_schema(schemas.TURN_SCHEMA, self.codex_dir / "schemas" / "turn.schema.json")
        images = [hero_abs] + [REPO_ROOT / path for path in views_rel.values()]

        thread_id: str | None = record.get("threadId")
        turn_index = len(record["turns"])
        last: TurnResult | None = None

        if thread_id is None:
            # ---- turn 1: intake → spec → blockout factory
            turn_index += 1
            log(f"stage build: turn {turn_index} — intake, spec and blockout factory (profile {profile}, target pass {target_pass})")
            text = prompts.build_start_turn(
                job_rel=self.job.rel,
                subject_name=subject_name,
                concept=self.job.state["concept"],
                profile=profile,
                complexity=complexity,
                target_pass=target_pass,
                reference_rel=hero_rel,
                extra_views=views_rel,
                camera=self.hero_camera,
                identity_features=list(data.get("identity_features") or []),
                materials=list(data.get("materials") or []),
                max_per_pass=int(self.settings.max_corrections_per_pass),
                max_total=int(self.settings.max_corrections_total),
                target_triangles=int(self.settings.target_triangles),
                factory_rel=factory_rel,
                language=self.settings.language,
            )
            result = self._codex_turn(text, label=f"build-{turn_index:02d}", images=images, schema=schema_path, resume=None)
            thread_id = result.thread_id or thread_id
            record["threadId"] = thread_id
            last = self._register_turn(record, turn_index, "start", result)
        else:
            log(f"stage build: resuming thread {thread_id[:8]}… at turn {turn_index}")
            last = self._synthetic_resume_turn(record)

        # ---- review loop
        max_turns = int(self.settings.max_review_turns)
        rendered_sha: str | None = record.get("renderedFactorySha")
        while True:
            if last is None:
                break
            if last.stage in {"blocked", "failed"} and not record.get("earlyBlockRetried") and self._resolve_factory(last, factory_rel) is None:
                # Giving up before a single factory exists is almost always a strict-quality
                # shortfall the model can fix by deepening the spec; spend one more turn on it.
                record["earlyBlockRetried"] = True
                turn_index += 1
                log("stage build: Codex stopped before producing a factory — asking once more to deepen the spec", level="warn")
                result = self._codex_turn(
                    "The pipeline expects at least a blockout factory before stopping. If the block is a strict-quality "
                    "shortfall, deepen the spec (more meso/micro components mapped from the detail inventory, repetition "
                    "systems, material localOverrides, subject-specific featureReviewTargets, review viewpoints) until "
                    f"`validate_sculpt_spec.py --strict-quality` passes, then generate {factory_rel} with --force and return "
                    "stage=\"factory-ready\". Return stage=\"blocked\" only if the subject truly cannot be reconstructed "
                    "from this image, and say why.",
                    label=f"build-{turn_index:02d}-retry",
                    images=[],
                    schema=schema_path,
                    resume=thread_id,
                )
                last = self._register_turn(record, turn_index, "repair", result)
                continue
            if last.stage in {"done", "blocked", "failed"}:
                break
            if last.stage == "invalid":
                if record.get("nudged", 0) >= 2:
                    log("stage build: Codex keeps returning non-JSON answers; stopping", level="warn")
                    break
                record["nudged"] = record.get("nudged", 0) + 1
                turn_index += 1
                result = self._codex_turn(
                    "Your last answer was not the required JSON. Reply now with ONLY the JSON object described by the output schema, "
                    "reflecting the real current state (stage, pass_id, factory_path, spec_path, factory_function, review, state_status, corrections_used, changed_files, message, message_ko).",
                    label=f"build-{turn_index:02d}-nudge",
                    images=[],
                    schema=schema_path,
                    resume=thread_id,
                )
                last = self._register_turn(record, turn_index, "nudge", result)
                continue

            # stage == factory-ready → render
            factory_abs = self._resolve_factory(last, factory_rel)
            if factory_abs is None:
                turn_index += 1
                result = self._codex_turn(
                    f"No factory file exists at {factory_rel} (or the path you reported). Generate it now with "
                    f"`python3 forge/stage3_build/generate_threejs_factory.py {spec_rel} --out {factory_rel} --force` "
                    "(fix the spec first if strict-quality blocks it) and return the JSON object with stage=\"factory-ready\".",
                    label=f"build-{turn_index:02d}-missing",
                    images=[],
                    schema=schema_path,
                    resume=thread_id,
                )
                last = self._register_turn(record, turn_index, "repair", result)
                continue
            reviews_done = sum(1 for turn in record["turns"] if turn.get("kind") == "review")
            if reviews_done >= max_turns:
                log(f"stage build: review budget ({max_turns}) exhausted before another render", level="warn")
                record["stopReason"] = "review budget exhausted"
                break
            if self._time_left() < 180:
                log("stage build: job time budget exhausted", level="warn")
                record["stopReason"] = "job time budget exhausted"
                break

            capture = self._render(factory_abs, last, hero_abs, profile, subject_name, f"turn-{turn_index:02d}")
            if capture is None:
                # render failed — ask Codex to repair the factory, counts as a review turn
                turn_index += 1
                error_text = record.get("lastRenderError", "unknown render error")
                result = self._codex_turn(
                    f"The pipeline could not render {factory_rel}:\n{error_text[:3000]}\n\n"
                    "Fix the cause (spec → regenerate with --force, or edit the factory for a runtime/bundle error), keep the pass unchanged, "
                    "and return the JSON object with stage=\"factory-ready\". Do not render anything yourself.",
                    label=f"build-{turn_index:02d}-renderfix",
                    images=[],
                    schema=schema_path,
                    resume=thread_id,
                )
                last = self._register_turn(record, turn_index, "review", result)
                continue
            rendered_sha = capture.get("factorySha256")
            record["renderedFactorySha"] = rendered_sha
            self.job.save()

            reviews_done = sum(1 for turn in record["turns"] if turn.get("kind") == "review")
            turns_left = max_turns - reviews_done - 1
            final = turns_left <= 0 or self._time_left() < self.settings.turn_timeout_min * 60 * 0.5
            turn_index += 1
            log(f"stage build: turn {turn_index} — review of pass {last.pass_id or '?'} ({'FINAL' if final else str(turns_left) + ' left'})")
            text = prompts.review_turn(
                job_rel=self.job.rel,
                pass_id=last.pass_id,
                target_pass=target_pass,
                capture=capture,
                gates_summary=capture.get("gatesSummary", ""),
                factory_rel=relpath(factory_abs),
                turn_index=turn_index,
                turns_left=max(0, turns_left),
                corrections_left=self._corrections_left(),
                reference_rel=hero_rel,
                final=final,
            )
            comparison = REPO_ROOT / capture["comparisonSheet"] if capture.get("comparisonSheet") else None
            result = self._codex_turn(
                text,
                label=f"build-{turn_index:02d}-review",
                images=[comparison] if comparison and comparison.is_file() else [],
                schema=schema_path,
                resume=thread_id,
            )
            last = self._register_turn(record, turn_index, "review", result)
            if final and last.stage == "factory-ready":
                record["stopReason"] = "review budget exhausted"
                break

        # ---- final render so preview.html reflects the last factory
        factory_abs = self._resolve_factory(last, factory_rel) if last else None
        if factory_abs is not None and factory_abs.is_file() and sha256_file(factory_abs) != rendered_sha:
            log("stage build: rendering the final factory state")
            self._render(factory_abs, last, hero_abs, profile, subject_name, "final")
        elif factory_abs is not None and factory_abs.is_file():
            # keep a 'final' history entry pointing at the last capture
            capture_json = self.job.path("preview", "capture.json")
            if capture_json.is_file():
                shutil.copyfile(capture_json, self.job.path("preview", "history", "final-capture.json"))

        progress = self._progress(REPO_ROOT / spec_rel, target_pass)
        record["progress"] = progress
        outcome = self._outcome(last, progress, record)
        record.update({"status": "done", "outcome": outcome, "finishedAt": now_iso(), "elapsedSec": round(self.clock.elapsed())})
        self.job.state["artifacts"].update(
            {
                "spec": spec_rel if (REPO_ROOT / spec_rel).is_file() else None,
                "factory": relpath(factory_abs) if factory_abs and factory_abs.is_file() else None,
                "previewHtml": relpath(self.job.path("preview", "preview.html")) if self.job.path("preview", "preview.html").is_file() else None,
                "comparison": relpath(self.job.path("preview", "cmp.png")) if self.job.path("preview", "cmp.png").is_file() else None,
                "heroRender": relpath(self.job.path("preview", "captures", "hero.png")) if self.job.path("preview", "captures", "hero.png").is_file() else None,
            }
        )
        self.job.state["status"] = outcome
        self.job.save()
        log(f"stage build: {outcome} — passes completed {progress.get('completedPasses')}, latest fidelity {progress.get('latestFidelity')}", level="ok" if outcome == "completed" else "warn")

    # ------------------------------------------------------------------ build helpers
    def _codex_turn(self, text: str, *, label: str, images: list[Path], schema: Path, resume: str | None) -> CodexResult:
        result = run_codex(
            self.settings,
            text,
            label=label,
            events_path=self.codex_dir / f"{label}.events.jsonl",
            last_message_path=self.codex_dir / f"{label}.last.json",
            prompt_path=self.codex_dir / f"{label}.prompt.md",
            output_schema=schema,
            resume_thread=resume,
            images=images,
            sandbox=self.settings.sandbox,
            network=self.settings.network_in_sandbox,
            cwd=REPO_ROOT,
            timeout_s=self._turn_timeout(),
        )
        self._record_usage(result)
        if result.timed_out:
            self.job.add_error(f"{label}: codex turn timed out")
        return result

    def _register_turn(self, record: dict[str, Any], index: int, kind: str, result: CodexResult) -> TurnResult:
        turn = TurnResult.from_codex(result)
        entry = {"index": index, "kind": kind, "at": now_iso(), **turn.summary()}
        record["turns"].append(entry)
        self.job.save()
        if turn.message_ko or turn.message:
            log(f"codex says: {turn.message_ko or turn.message}")
        if turn.stage == "invalid":
            log(f"turn {index}: no valid JSON stage in the final message", level="warn")
        return turn

    def _synthetic_resume_turn(self, record: dict[str, Any]) -> TurnResult | None:
        """When resuming an interrupted build, pretend the last recorded turn just happened so the
        loop re-renders and continues from there."""
        turns = record.get("turns") or []
        if not turns:
            return None
        last = turns[-1]
        dummy = CodexResult(
            returncode=0,
            thread_id=record.get("threadId"),
            last_message="",
            structured={
                "stage": last.get("stage") if last.get("stage") in {"factory-ready", "done", "blocked", "failed"} else "factory-ready",
                "pass_id": last.get("passId"),
                "factory_path": last.get("factoryPath") or record.get("factoryPath"),
                "spec_path": last.get("specPath") or record.get("specPath"),
                "factory_function": last.get("factoryFunction"),
                "review": last.get("review"),
                "state_status": last.get("stateStatus"),
                "corrections_used": last.get("correctionsUsed"),
                "changed_files": [],
                "message": "resumed",
                "message_ko": "재개",
            },
            usage={},
            duration=0.0,
            timed_out=False,
            events_path=None,
        )
        turn = TurnResult.from_codex(dummy)
        if turn.stage in {"done", "blocked", "failed"} and record.get("outcome"):
            return turn
        # force a re-render on resume
        record["renderedFactorySha"] = None
        turn.stage = "factory-ready"
        return turn

    def _resolve_factory(self, turn: TurnResult | None, default_rel: str) -> Path | None:
        candidates: list[Path] = []
        if turn and turn.factory_path:
            reported = Path(turn.factory_path)
            candidates.append(reported if reported.is_absolute() else REPO_ROOT / reported)
        candidates.append(REPO_ROOT / default_rel)
        src_dir = self.job.path("src")
        if src_dir.is_dir():
            candidates.extend(sorted(src_dir.glob("*.ts"), key=lambda p: p.stat().st_mtime, reverse=True))
        for candidate in candidates:
            if candidate.is_file() and candidate.stat().st_size > 200:
                return candidate.resolve()
        return None

    def _render(self, factory: Path, turn: TurnResult | None, hero: Path, profile: str, subject: str, tag: str) -> dict[str, Any] | None:
        spec_path = self.job.path("object-sculpt-spec.json")
        try:
            capture = render_factory(
                factory=factory,
                out_dir=self.job.path("preview"),
                settings=self.settings,
                reference=hero,
                spec=spec_path if spec_path.is_file() else None,
                pass_id=turn.pass_id if turn else None,
                hero=self.hero_camera,
                character=profile == "character",
                title=subject,
                history_tag=tag,
            )
        except Auto3DError as exc:
            log(f"render failed: {exc}", level="warn")
            self.job.stage("build")["lastRenderError"] = str(exc)
            self.job.add_error(f"render {tag}: {exc}")
            self.job.save()
            return None
        self.job.stage("build")["lastRenderError"] = None
        self.job.stage("build").setdefault("renders", []).append(
            {
                "tag": tag,
                "at": now_iso(),
                "passId": capture.get("passId"),
                "factorySha256": capture.get("factorySha256"),
                "triangles": capture.get("triangles"),
                "comparisonSheet": capture.get("comparisonSheet"),
                "gates": {name: _gate_verdict(gate) for name, gate in (capture.get("gates") or {}).items()},
            }
        )
        self.job.save()
        return capture

    def _corrections_left(self) -> int | None:
        state_path = self.job.path(".img2threejs", "state.json")
        if not state_path.is_file():
            return None
        try:
            state = read_json(state_path)
        except ValueError:
            return None
        loop = state.get("loop") or {}
        max_total = loop.get("maxTotal") or self.settings.max_corrections_total
        used = loop.get("totalCount")
        if isinstance(used, int) and isinstance(max_total, int):
            return max(0, max_total - used)
        return None

    def _progress(self, spec_path: Path, target_pass: str) -> dict[str, Any]:
        progress: dict[str, Any] = {"specExists": spec_path.is_file(), "completedPasses": [], "latestReview": None, "latestFidelity": None, "targetPassPassed": False, "reviewCount": 0}
        if not spec_path.is_file():
            return progress
        try:
            spec = read_json(spec_path)
        except ValueError:
            progress["error"] = "spec is not valid JSON"
            return progress
        history = spec.get("reviewHistory") or []
        history = [entry for entry in history if isinstance(entry, dict)]
        progress["reviewCount"] = len(history)
        completed: list[str] = []
        for pass_id in PASS_ORDER:
            if any(entry.get("passId") == pass_id and entry.get("action") == "continue" for entry in history):
                completed.append(pass_id)
            else:
                break
        progress["completedPasses"] = completed
        progress["targetPassPassed"] = target_pass in completed
        if history:
            latest = history[-1]
            progress["latestReview"] = {
                "passId": latest.get("passId"),
                "action": latest.get("action"),
                "fidelity": latest.get("estimatedFidelity"),
                "aiVisionScore": latest.get("aiVisionScore"),
                "summary": latest.get("summary"),
                "timestamp": latest.get("timestamp"),
            }
            progress["latestFidelity"] = latest.get("estimatedFidelity")
        progress["reviews"] = [
            {
                "passId": entry.get("passId"),
                "action": entry.get("action"),
                "fidelity": entry.get("estimatedFidelity"),
                "aiVisionScore": entry.get("aiVisionScore"),
                "summary": entry.get("summary"),
                "timestamp": entry.get("timestamp"),
                "layerScores": entry.get("layerScores"),
                "mismatches": entry.get("mismatches"),
            }
            for entry in history
        ]
        progress["targetName"] = spec.get("targetName")
        progress["componentCount"] = len(spec.get("componentTree") or [])
        progress["materialCount"] = len(spec.get("materials") or [])
        return progress

    def _outcome(self, last: TurnResult | None, progress: dict[str, Any], record: dict[str, Any]) -> str:
        if progress.get("targetPassPassed"):
            return "completed"
        if last is None:
            return "failed"
        if last.stage == "done":
            # Codex claimed done but the spec disagrees — trust the spec.
            log("Codex reported done but the spec's reviewHistory does not show the target pass as continue; marking partial", level="warn")
            return "partial"
        if last.stage == "failed":
            return "failed"
        if last.stage == "blocked":
            return "blocked" if not progress.get("completedPasses") else "partial"
        if record.get("stopReason"):
            return "partial"
        return "partial" if progress.get("specExists") else "failed"

    # ------------------------------------------------------------------ stage: report
    def stage_report(self) -> None:
        from .report import write_job_report, write_gallery

        record = self.job.stage("report")
        record.update({"status": "running", "startedAt": now_iso()})
        report_path = write_job_report(self.job, self.settings)
        gallery = write_gallery(self.settings)
        record.update({"status": "done", "finishedAt": now_iso(), "path": relpath(report_path), "gallery": relpath(gallery) if gallery else None})
        self.job.state["artifacts"]["report"] = relpath(report_path)
        if self.job.state.get("status") in {"running", "created"}:
            self.job.state["status"] = self.job.stage("build").get("outcome") or "partial"
        self.job.save()
        log(f"report: {relpath(report_path)}" + (f" · gallery {relpath(gallery)}" if gallery else ""), level="ok")


def _gate_verdict(gate: Any) -> Any:
    if not isinstance(gate, dict):
        return None
    result = gate.get("result")
    if isinstance(result, dict):
        if "passed" in result:
            return "PASS" if result["passed"] else "FAIL"
        if "selfIntersecting" in result:
            return "FAIL" if result["selfIntersecting"] else "PASS"
        if "degenerate" in result:
            return "FAIL" if result["degenerate"] else "PASS"
        return "recorded"
    if "ok" in gate:
        return "PASS" if gate.get("ok") else ("FAIL" if gate.get("ok") is False else "n/a")
    return "ERROR"


def _stub_reference_prompt(concept: str, name: str | None, views: list[str]) -> dict[str, Any]:
    """Minimal prompt.json for the `--reference` path when no intake turn ran (prompt_author=
    template, or the intake turn failed). The build turn still reads the images itself, so this
    only has to name the subject and stay out of the way — it must not invent identity features."""
    subject = (name or concept or "Supplied Reference").strip()[:80]
    return _fill_prompt_defaults(
        {
            "subject_name": subject,
            "subject_slug": slugify(subject),
            "image_prompt": (
                "Use case: supplied-reference\n"
                "Asset type: 3D reconstruction reference image\n"
                f"Primary request: rebuild the subject shown in the supplied reference image(s){f' ({subject})' if subject else ''}.\n"
                "Note: no intake description was produced — read the attached reference images directly.\n"
            ),
            "notes_ko": "참조 이미지를 그대로 사용했고, 인테이크 분석 턴은 실행되지 않았습니다.",
        },
        views,
    )


def _fill_prompt_defaults(data: dict[str, Any], views: list[str]) -> dict[str, Any]:
    data.setdefault("subject_name", "Subject")
    data.setdefault("subject_slug", slugify(str(data["subject_name"])))
    if data.get("profile") not in {"generic", "character"}:
        data["profile"] = "generic"
    if data.get("complexity") not in {"simple", "moderate", "complex", "ultra-complex"}:
        data["complexity"] = "moderate"
    view_prompts = data.get("view_prompts") if isinstance(data.get("view_prompts"), dict) else {}
    data["view_prompts"] = {name: view_prompts.get(name) for name in ("front", "side", "back", "top")}
    for key in ("identity_features", "materials", "avoid"):
        if not isinstance(data.get(key), list):
            data[key] = []
    data.setdefault("notes_ko", "")
    return data


def describe_job(job: Job) -> str:
    """One-paragraph console summary after a run."""
    build = job.stage("build")
    progress = build.get("progress") or {}
    usage = job.state.get("usage") or {}
    parts = [
        f"job {job.id}: {job.state.get('status')}",
        f"subject: {job.state.get('subject')}",
        f"passes: {', '.join(progress.get('completedPasses') or []) or 'none'} (target {build.get('targetPass')})",
        f"fidelity: {progress.get('latestFidelity')}",
        f"tokens: in {usage.get('input_tokens', 0):,} / out {usage.get('output_tokens', 0):,}",
        f"elapsed: {human_duration(build.get('elapsedSec') or 0)}",
    ]
    artifacts = job.state.get("artifacts") or {}
    for key in ("previewHtml", "report", "factory", "spec"):
        if artifacts.get(key):
            parts.append(f"{key}: {artifacts[key]}")
    return "\n  ".join(parts)
