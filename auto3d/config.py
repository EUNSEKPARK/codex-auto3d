"""Configuration: defaults ← auto3d.config.json ← environment ← CLI flags."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util import INTEGRATION_ROOT, REPO_ROOT, Auto3DError, read_json

CONFIG_FILE = INTEGRATION_ROOT / "auto3d.config.json"

# Build passes in forge order (forge/stage3_build/orchestrate_passes.py::DEFAULT_PASS_ORDER).
PASS_ORDER = [
    "blockout",
    "structural-pass",
    "form-refinement",
    "material-pass",
    "surface-pass",
    "lighting-pass",
    "interaction-pass",
    "optimization-pass",
]

# Quality presets map to the last pass that must be reviewed `continue`.
QUALITY_TARGET_PASS = {
    "draft": "form-refinement",
    "standard": "material-pass",
    "full": "optimization-pass",
}

DEFAULTS: dict[str, Any] = {
    # Codex
    "codex_bin": "codex",
    "model": "",  # empty = Codex default
    "reasoning_effort": "",  # empty = Codex default; e.g. "high"
    "sandbox": "workspace-write",  # workspace-write | danger-full-access
    "network_in_sandbox": False,
    "turn_timeout_min": 60,
    # The first build turn does intake, assessment, detail inventory, spec authoring, strict
    # validation and the blockout factory in one go. A character run measured over 60 minutes and
    # was killed mid-flight; it only survived because the factory happened to be written already.
    "first_turn_timeout_min": 120,
    "job_timeout_min": 300,
    # prompt authoring
    "prompt_author": "codex",  # codex | template
    "style": "stylized 3D render, clean game-asset look",
    "language": "ko",  # language for human-facing summaries
    # image generation
    "image_backend": "codex",  # codex | api | auto
    "image_model": "gpt-image-2",
    "image_size": "1024x1024",
    "image_quality": "high",
    "image_attempts": 3,
    "views": [],  # extra reference views: front, side, back, top
    "hero_azimuth": 35.0,
    "hero_elevation": 15.0,
    # 3D pipeline
    "profile": "auto",  # auto | generic | character
    "quality": "standard",  # draft | standard | full
    "complexity": "auto",  # auto | simple | moderate | complex | ultra-complex
    "max_review_turns": 12,
    # A character blockout has to converge silhouette AND proportion before it can pass; 3 per
    # pass hard-stopped a run that was still improving (0.43 → 0.62 fidelity). max_review_turns
    # is the real ceiling on cost, so these can be generous.
    "max_corrections_per_pass": 5,
    "max_corrections_total": 10,
    "target_triangles": 60000,
    # preview / capture
    "viewport": [900, 900],
    "background": "#f2f2f2",
    "device_pixel_ratio": 1.0,
    # layout
    "work_root": "work/auto3d",
}

ENV_PREFIX = "AUTO3D_"


@dataclass
class Settings:
    values: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))

    def __getattr__(self, name: str) -> Any:  # convenience: settings.codex_bin
        values = self.__dict__.get("values", {})
        if name in values:
            return values[name]
        raise AttributeError(name)

    def get(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)

    def set(self, name: str, value: Any) -> None:
        self.values[name] = value

    # derived ------------------------------------------------------------
    @property
    def work_root_path(self) -> Path:
        root = Path(self.values["work_root"])
        return root if root.is_absolute() else REPO_ROOT / root

    @property
    def target_pass(self) -> str:
        quality = self.values["quality"]
        if quality in PASS_ORDER:
            return quality
        try:
            return QUALITY_TARGET_PASS[quality]
        except KeyError as exc:
            raise Auto3DError(f"unknown quality preset: {quality!r} (draft|standard|full or a pass id)") from exc

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.values)


def _coerce(name: str, raw: str) -> Any:
    default = DEFAULTS.get(name)
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int) and not isinstance(default, bool):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, list):
        return [part.strip() for part in raw.split(",") if part.strip()]
    return raw


def load_settings(config_path: Path | None = None, overrides: dict[str, Any] | None = None) -> Settings:
    settings = Settings()
    path = config_path or CONFIG_FILE
    if path.is_file():
        try:
            data = read_json(path)
        except ValueError as exc:
            raise Auto3DError(f"config file is not valid JSON: {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise Auto3DError(f"config file must contain a JSON object: {path}")
        for key, value in data.items():
            if key.startswith("_"):
                continue
            settings.set(key, value)
    for key in DEFAULTS:
        env_key = ENV_PREFIX + key.upper()
        if env_key in os.environ:
            settings.set(key, _coerce(key, os.environ[env_key]))
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        settings.set(key, value)
    validate_settings(settings)
    return settings


def validate_settings(settings: Settings) -> None:
    values = settings.values
    if values["sandbox"] not in {"workspace-write", "danger-full-access", "read-only"}:
        raise Auto3DError("sandbox must be workspace-write, danger-full-access or read-only")
    if values["image_backend"] not in {"codex", "api", "auto"}:
        raise Auto3DError("image_backend must be codex, api or auto")
    if values["prompt_author"] not in {"codex", "template"}:
        raise Auto3DError("prompt_author must be codex or template")
    if values["profile"] not in {"auto", "generic", "character"}:
        raise Auto3DError("profile must be auto, generic or character")
    if values["complexity"] not in {"auto", "simple", "moderate", "complex", "ultra-complex"}:
        raise Auto3DError("complexity must be auto, simple, moderate, complex or ultra-complex")
    _ = settings.target_pass  # raises on a bad preset
    views = values.get("views") or []
    if isinstance(views, str):
        views = [part.strip() for part in views.split(",") if part.strip()]
        values["views"] = views
    for view in views:
        if view not in VIEW_CAMERAS:
            raise Auto3DError(f"unknown extra view {view!r}; choose from {', '.join(VIEW_CAMERAS)}")
    viewport = values.get("viewport")
    if not (isinstance(viewport, list) and len(viewport) == 2):
        raise Auto3DError("viewport must be [width, height]")


# Camera azimuth/elevation (degrees) for each named reference view. Azimuth 0 looks at the
# subject's front (+Z), positive azimuth walks the camera toward the subject's own left (+X) —
# the same convention as the generated frame<Name>Camera helper and forge/_shared/chirality.py.
VIEW_CAMERAS: dict[str, dict[str, float]] = {
    "hero": {"azimuth": 35.0, "elevation": 15.0},
    "front": {"azimuth": 0.0, "elevation": 0.0},
    "side": {"azimuth": 90.0, "elevation": 0.0},
    "back": {"azimuth": 180.0, "elevation": 0.0},
    "top": {"azimuth": 0.0, "elevation": 80.0},
}

VIEW_DESCRIPTIONS: dict[str, str] = {
    "front": "straight-on front orthographic-style view (camera directly in front, at the subject's mid-height)",
    "side": "straight-on left profile view (camera at 90 degrees, at the subject's mid-height)",
    "back": "straight-on rear view (camera directly behind, at the subject's mid-height)",
    "top": "top-down view (camera almost directly above)",
}
