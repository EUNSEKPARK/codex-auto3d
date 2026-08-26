"""Preview build + deterministic browser capture for a generated img2threejs factory.

Pipeline: factory.ts ──esbuild──▶ bundle.js ──▶ preview.html (self-contained, interactive)
          preview.html ──Playwright (headless Chromium)──▶ hero / orbit / turntable PNGs + meshes.json
          PNGs ──forge gates──▶ comparison sheet, turntable gate, self-intersection, Tier-1 diagnostics

The forge scripts remain the authority for every gate; this module only produces the pixels and
the evidence files they consume, then summarises their verdicts for the review turn.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings, VIEW_CAMERAS
from .util import (
    Auto3DError,
    FORGE,
    INTEGRATION_ROOT,
    REPO_ROOT,
    debug,
    log,
    parse_json_output,
    read_text,
    relpath,
    run,
    run_forge,
    sha256_file,
    write_json,
    write_text,
)

NODE_DIR = INTEGRATION_ROOT / "node"
VENV_DIR = INTEGRATION_ROOT / ".venv"
VIEWER_DIR = INTEGRATION_ROOT / "viewer"
WORKER = Path(__file__).resolve().parent / "capture_worker.py"

EXPORT_PATTERN = re.compile(r"^export function (create(\w+?)Model)\(", re.MULTILINE)


# ---------------------------------------------------------------------------
# toolchain discovery
# ---------------------------------------------------------------------------


def esbuild_bin() -> Path | None:
    candidates = [NODE_DIR / "node_modules" / ".bin" / ("esbuild.cmd" if os.name == "nt" else "esbuild")]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    found = shutil.which("esbuild")
    return Path(found) if found else None


def tsc_bin() -> Path | None:
    candidate = NODE_DIR / "node_modules" / ".bin" / ("tsc.cmd" if os.name == "nt" else "tsc")
    return candidate if candidate.exists() else None


def three_installed() -> bool:
    return (NODE_DIR / "node_modules" / "three" / "package.json").is_file()


def playwright_python() -> Path | None:
    """Python interpreter that can import Playwright: the integration venv first, then the
    current interpreter."""
    for candidate in (
        VENV_DIR / "bin" / "python",
        VENV_DIR / "Scripts" / "python.exe",
    ):
        if candidate.exists():
            return candidate
    try:
        import importlib.util

        if importlib.util.find_spec("playwright") is not None:
            return Path(sys.executable)
    except (ImportError, ValueError):
        pass
    return None


# ---------------------------------------------------------------------------
# factory inspection + bundling
# ---------------------------------------------------------------------------


@dataclass
class FactoryExports:
    type_name: str
    factory_fn: str
    lights_fn: str
    env_fn: str
    frame_fn: str
    configure_fn: str
    controls_fn: str

    def as_config(self) -> dict[str, str]:
        return {
            "factoryFn": self.factory_fn,
            "lightsFn": self.lights_fn,
            "envFn": self.env_fn,
            "frameFn": self.frame_fn,
            "configureFn": self.configure_fn,
            "controlsFn": self.controls_fn,
        }


def detect_exports(factory_source: str) -> FactoryExports:
    match = EXPORT_PATTERN.search(factory_source)
    if not match:
        raise Auto3DError("factory does not export a `create<Name>Model` function")
    factory_fn, type_name = match.group(1), match.group(2)
    return FactoryExports(
        type_name=type_name,
        factory_fn=factory_fn,
        lights_fn=f"create{type_name}LookDevLights",
        env_fn=f"create{type_name}Environment",
        frame_fn=f"frame{type_name}Camera",
        configure_fn=f"configure{type_name}Renderer",
        controls_fn=f"create{type_name}InspectControls",
    )


ENTRY_TEMPLATE = """import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import * as Factory from './factory';
(window as any).__AUTO3D_THREE__ = THREE;
(window as any).__AUTO3D_FACTORY__ = Factory;
(window as any).__AUTO3D_ORBIT_CONTROLS__ = OrbitControls;
"""


def build_bundle(factory_path: Path, build_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Bundle the factory (+ three) into one IIFE script. Returns (bundle_path, info)."""
    binary = esbuild_bin()
    if binary is None or not three_installed():
        raise Auto3DError("esbuild/three are not installed — run `python3 auto3d.py setup`")
    build_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(factory_path, build_dir / "factory.ts")
    write_text(build_dir / "entry.ts", ENTRY_TEMPLATE)
    bundle = build_dir / "bundle.js"
    completed = run(
        [
            str(binary),
            str(build_dir / "entry.ts"),
            "--bundle",
            "--format=iife",
            "--target=es2020",
            "--log-level=warning",
            f"--outfile={bundle}",
        ],
        cwd=NODE_DIR,
        timeout=180,
        env={"NODE_PATH": str(NODE_DIR / "node_modules")},
    )
    info = {"returncode": completed.returncode, "stderr": completed.stderr.strip()[-4000:]}
    if completed.returncode != 0 or not bundle.is_file():
        raise Auto3DError(f"esbuild failed:\n{completed.stderr.strip()[-3000:]}")
    info["bytes"] = bundle.stat().st_size
    return bundle, info


def typecheck(build_dir: Path) -> dict[str, Any]:
    """Optional `tsc --noEmit` over the copied factory. Never blocks; evidence only."""
    binary = tsc_bin()
    if binary is None:
        return {"available": False, "ok": None, "errors": [], "note": "typescript not installed in node/"}
    tsconfig = {
        "compilerOptions": {
            "target": "es2020",
            "module": "esnext",
            "moduleResolution": "bundler",
            "strict": True,
            "noEmit": True,
            "skipLibCheck": True,
            "esModuleInterop": True,
            "noUnusedLocals": False,
            "types": [],
            "baseUrl": str(NODE_DIR),
            "paths": {
                "three": [str(NODE_DIR / "node_modules" / "@types" / "three" / "index.d.ts")],
                "three/examples/jsm/*": [str(NODE_DIR / "node_modules" / "@types" / "three" / "examples" / "jsm" / "*")],
                "three/*": [str(NODE_DIR / "node_modules" / "@types" / "three" / "*")],
            },
        },
        "files": [str(build_dir / "factory.ts")],
    }
    tsconfig_path = build_dir / "tsconfig.json"
    write_json(tsconfig_path, tsconfig)
    completed = run([str(binary), "-p", str(tsconfig_path), "--pretty", "false"], cwd=NODE_DIR, timeout=300)
    lines = [line for line in (completed.stdout + completed.stderr).splitlines() if "error TS" in line]
    return {"available": True, "ok": completed.returncode == 0, "errors": lines[:40], "errorCount": len(lines)}


def _inline_js(source: str) -> str:
    return source.replace("</script", "<\\/script")


def write_preview_html(bundle: Path, config: dict[str, Any], out_html: Path) -> Path:
    template = read_text(VIEWER_DIR / "viewer.html")
    viewer_js = read_text(VIEWER_DIR / "viewer.js")
    html = (
        template.replace("__AUTO3D_TITLE__", str(config.get("title", "preview")))
        .replace("__AUTO3D_BACKGROUND__", str(config.get("background", "#f2f2f2")))
        .replace("__AUTO3D_CONFIG_JSON__", json.dumps(config, ensure_ascii=False))
        .replace("__AUTO3D_BUNDLE__", _inline_js(read_text(bundle)))
        .replace("__AUTO3D_VIEWER_JS__", _inline_js(viewer_js))
    )
    write_text(out_html, html)
    return out_html


# ---------------------------------------------------------------------------
# capture plan
# ---------------------------------------------------------------------------


def capture_plan(hero: dict[str, float], character: bool) -> list[dict[str, Any]]:
    az, el = float(hero["azimuth"]), float(hero["elevation"])
    plan = [
        {"id": "hero", "role": "reference-match", "azimuth": az, "elevation": el},
        {"id": "hero-mapstripped", "role": "reference-match", "azimuth": az, "elevation": el, "mapStripped": True},
        {"id": "orbit-plus35", "role": "orbit", "azimuth": 35.0, "elevation": 0.0},
        {"id": "orbit-minus35", "role": "orbit", "azimuth": -35.0, "elevation": 0.0},
        {"id": "profile", "role": "orbit", "azimuth": 78.0, "elevation": 0.0},
        {"id": "rear", "role": "orbit", "azimuth": 180.0, "elevation": 0.0},
        {"id": "az000", "role": "turntable", "azimuth": 0.0, "elevation": 0.0},
        {"id": "az090", "role": "turntable", "azimuth": 90.0, "elevation": 0.0},
        {"id": "az270", "role": "turntable", "azimuth": 270.0, "elevation": 0.0},
        {"id": "top", "role": "orbit", "azimuth": 0.0, "elevation": 80.0},
        {"id": "head-hero", "role": "head-closeup", "azimuth": 0.0, "elevation": 0.0},
        {"id": "head-threequarter", "role": "head-closeup", "azimuth": 35.0, "elevation": 0.0},
    ]
    if not character:
        # Objects still get the close-ups (they frame the top part), they are cheap and the
        # render bridge manifest expects the ids to exist.
        pass
    return plan


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------


def run_capture(html: Path, out_dir: Path, plan: list[dict[str, Any]], settings: Settings, *, meshes_out: Path | None) -> dict[str, Any]:
    python = playwright_python()
    if python is None:
        raise Auto3DError("Playwright is not installed — run `python3 auto3d.py setup`")
    out_dir.mkdir(parents=True, exist_ok=True)
    job = {
        "html": str(html.resolve()),
        "viewport": list(settings.viewport),
        "devicePixelRatio": float(settings.device_pixel_ratio),
        "timeoutMs": 90000,
        "captures": [dict(item, path=str((out_dir / f"{item['id']}.png").resolve())) for item in plan],
        "meshesOut": str(meshes_out.resolve()) if meshes_out else None,
        "maxTriangles": 400000,
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(job, handle)
        job_path = Path(handle.name)
    try:
        env = {}
        if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            env["PLAYWRIGHT_BROWSERS_PATH"] = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
        completed = run([str(python), str(WORKER), str(job_path)], cwd=REPO_ROOT, timeout=600, env=env)
    finally:
        job_path.unlink(missing_ok=True)
    payload = parse_json_output(completed.stdout)
    if not isinstance(payload, dict):
        raise Auto3DError(f"capture worker produced no result (exit {completed.returncode}):\n{completed.stderr.strip()[-2000:]}")
    if not payload.get("ok"):
        raise Auto3DError(f"browser capture failed: {payload.get('error')}\nconsole: {payload.get('consoleErrors')}\npage: {payload.get('pageErrors')}")
    return payload


# ---------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------


def _json_gate(script: str, *args: object, out_path: Path, timeout: float = 600) -> dict[str, Any]:
    completed = run_forge(script, *args, timeout=timeout)
    payload = parse_json_output(completed.stdout)
    record: dict[str, Any] = {
        "script": script,
        "returncode": completed.returncode,
        "path": relpath(out_path),
    }
    if isinstance(payload, dict):
        record["result"] = payload
    else:
        record["result"] = None
        record["stderr"] = completed.stderr.strip()[-1500:]
        record["stdout"] = completed.stdout.strip()[-1500:]
    write_json(out_path, record)
    return record


def run_gates(
    *,
    reference: Path | None,
    captures: dict[str, Path],
    meshes: Path | None,
    gates_dir: Path,
    comparison_out: Path,
) -> dict[str, Any]:
    gates_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    hero = captures.get("hero")

    if reference is not None and hero is not None:
        sheet = _json_gate(
            "stage4_review/make_comparison_sheet.py",
            "--reference", reference, "--render", hero, "--out", comparison_out, "--json",
            out_path=gates_dir / "comparison_sheet.json",
        )
        results["comparisonSheet"] = sheet
        results["tier1"] = _json_gate(
            "stage4_review/diagnose_render.py",
            "--reference", reference, "--render", hero, "--json",
            out_path=gates_dir / "tier1.json",
        )
        results["interiorDifference"] = _json_gate(
            "stage4_review/interior_difference.py", reference, hero, "--json",
            out_path=gates_dir / "interior_difference.json",
        )
    if hero is not None:
        # `--reference` here is the fixed-view RENDER; the orbit views must not collapse against it.
        orbit = [captures[name] for name in ("orbit-plus35", "orbit-minus35", "profile", "rear") if name in captures]
        if orbit:
            results["multiAngle"] = _json_gate(
                "stage4_review/diagnose_render_multi_angle.py", "--reference", hero,
                *[arg for path in orbit for arg in ("--orbit", path)], "--json",
                out_path=gates_dir / "multi_angle.json",
            )

    turntable_args: list[object] = []
    for azimuth, name in ((0, "az000"), (90, "az090"), (180, "rear"), (270, "az270")):
        if name in captures:
            turntable_args += ["--capture", f"{azimuth}={captures[name]}"]
    if turntable_args:
        results["turntable"] = _json_gate(
            "stage4_review/turntable_gate.py", *turntable_args, "--json", out_path=gates_dir / "turntable.json"
        )

    if meshes is not None and meshes.is_file():
        results["selfIntersection"] = _json_gate(
            "stage4_review/self_intersection.py", meshes, "--json", out_path=gates_dir / "self_intersection.json", timeout=900
        )
    return results


def summarize_gates(gates: dict[str, Any]) -> str:
    """One line per gate for the review prompt (Codex reads the JSON files for detail)."""
    lines: list[str] = []

    def verdict(record: dict[str, Any], passed_key: str = "passed") -> str:
        result = record.get("result")
        if not isinstance(result, dict):
            return f"ERROR (exit {record.get('returncode')})"
        if passed_key in result:
            return "PASS" if result[passed_key] else "FAIL"
        return "recorded"

    if "tier1" in gates:
        result = gates["tier1"].get("result") or {}
        checks = result.get("checks") or {}
        lines.append(
            f"- diagnose_render Tier-1 (hero vs reference): {verdict(gates['tier1'])} · IoU={checks.get('silhouetteIoU')} "
            f"aspectΔ={checks.get('aspectRatioDelta')} scaleΔ={checks.get('scaleDelta')} · failures={result.get('failures') or []} → {gates['tier1']['path']}"
        )
    if "multiAngle" in gates:
        result = gates["multiAngle"].get("result") or {}
        lines.append(f"- multi-angle: degenerate={result.get('degenerate')} → {gates['multiAngle']['path']}")
    if "interiorDifference" in gates:
        result = gates["interiorDifference"].get("result") or {}
        value = result.get("interiorDifference")
        lines.append(
            f"- interior_difference (inside the silhouette, lower is closer): "
            f"{round(value, 4) if isinstance(value, (int, float)) else 'n/a'} · status={result.get('status')} "
            f"cells={result.get('cellsCompared')} → {gates['interiorDifference']['path']}"
        )
    if "turntable" in gates:
        result = gates["turntable"].get("result") or {}
        lines.append(
            f"- turntable_gate (0/90/180/270): {verdict(gates['turntable'])} · missingAzimuths={result.get('missingAzimuths')} "
            f"degenerate={result.get('degenerate')} → {gates['turntable']['path']}"
        )
    if "selfIntersection" in gates:
        result = gates["selfIntersection"].get("result") or {}
        state = "FAIL" if result.get("selfIntersecting") else ("PASS" if result else "ERROR")
        lines.append(
            f"- self_intersection: {state} · meshes={result.get('meshCount')} inside={result.get('insideVertexCount')} "
            f"sampled={result.get('sampledVertexCount')}/{result.get('totalVertexCount')} → {gates['selfIntersection']['path']}"
        )
    if "typecheck" in gates:
        result = gates["typecheck"]
        if result.get("available"):
            lines.append(f"- tsc --noEmit: {'ok' if result.get('ok') else str(result.get('errorCount')) + ' errors'}" + (f" (first: {result['errors'][0]})" if result.get("errors") else ""))
    return "\n".join(lines) or "- (no gates were run)"


# ---------------------------------------------------------------------------
# render manifest (forge/stage4_review/render_bridge.py evidence format)
# ---------------------------------------------------------------------------


def write_render_manifest(
    *,
    reference: Path,
    html: Path,
    manifest_path: Path,
    settings: Settings,
    captured: list[dict[str, Any]],
    console_errors: list[str],
) -> Path | None:
    sys.path.insert(0, str(FORGE / "stage4_review"))
    try:
        import render_bridge  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        debug(f"render_bridge unavailable: {exc}")
        return None
    try:
        manifest = render_bridge.init_manifest(
            reference,
            html.resolve().as_uri(),
            manifest_path,
            (int(settings.viewport[0]), int(settings.viewport[1])),
            float(settings.device_pixel_ratio),
            "captures",
        )
        known = {item["id"] for item in render_bridge.CAPTURE_PLAN}
        for item in captured:
            if item.get("id") in known:
                render_bridge.record_capture(
                    manifest_path,
                    manifest,
                    item["id"],
                    Path(item["path"]),
                    ready_signal=True,
                    console_errors=console_errors,
                    browser_snapshot={"canvas": item.get("canvas"), "camera": {"azimuth": item.get("azimuth"), "elevation": item.get("elevation"), "distance": item.get("distance"), "fov": item.get("fov")}},
                )
        manifest["evidence"]["browser"] = {"adapter": "playwright", "browser": "chromium", "headless": True, "via": "codex_auto3d"}
        manifest["evidence"]["consoleErrors"] = console_errors
        render_bridge.write_manifest(manifest_path, manifest)
        return manifest_path
    except Exception as exc:  # noqa: BLE001
        log(f"render manifest not written: {exc}", level="warn")
        return None


# ---------------------------------------------------------------------------
# top level
# ---------------------------------------------------------------------------


def render_factory(
    *,
    factory: Path,
    out_dir: Path,
    settings: Settings,
    reference: Path | None = None,
    spec: Path | None = None,
    pass_id: str | None = None,
    hero: dict[str, float] | None = None,
    character: bool = False,
    title: str | None = None,
    history_tag: str | None = None,
) -> dict[str, Any]:
    """Build + capture + gate one factory. Returns the capture summary (also written to
    out_dir/capture.json)."""
    factory = factory.resolve()
    if not factory.is_file():
        raise Auto3DError(f"factory not found: {factory}")
    out_dir.mkdir(parents=True, exist_ok=True)
    source = read_text(factory)
    exports = detect_exports(source)
    hero_cam = dict(hero or VIEW_CAMERAS["hero"])

    log(f"preview: bundling {relpath(factory)} ({exports.factory_fn})")
    build_dir = out_dir / "build"
    bundle, bundle_info = build_bundle(factory, build_dir)
    tsc_info = typecheck(build_dir)
    if tsc_info.get("available") and not tsc_info.get("ok"):
        log(f"preview: tsc reported {tsc_info.get('errorCount')} error(s) (non-blocking)", level="warn")

    config = {
        **exports.as_config(),
        "title": title or exports.type_name,
        "passId": pass_id,
        "background": settings.background,
        "hero": hero_cam,
        "views": {name: dict(cam) for name, cam in VIEW_CAMERAS.items()} | {"hero": hero_cam},
        "fovDegrees": 35,
        "margin": 1.12,
        "groundShadow": True,
    }
    html = write_preview_html(bundle, config, out_dir / "preview.html")
    # Interactive copy with the bundle shared (smaller than duplicating): preview.html already
    # works in both modes, capture mode is selected by ?capture=1.

    captures_dir = out_dir / "captures"
    for stale in captures_dir.glob("*.png"):
        stale.unlink()
    meshes_path = out_dir / "meshes.json"
    plan = capture_plan(hero_cam, character)
    log(f"preview: capturing {len(plan)} views in headless Chromium")
    payload = run_capture(html, captures_dir, plan, settings, meshes_out=meshes_path)
    captured = payload.get("captures", [])
    console_errors = list(payload.get("consoleErrors", [])) + list(payload.get("pageErrors", []))
    if console_errors:
        log(f"preview: browser reported {len(console_errors)} console error(s)", level="warn")
    capture_paths = {item["id"]: Path(item["path"]) for item in captured}

    comparison = out_dir / "cmp.png"
    gates = run_gates(
        reference=reference,
        captures=capture_paths,
        meshes=meshes_path if meshes_path.is_file() else None,
        gates_dir=out_dir / "gates",
        comparison_out=comparison,
    )
    gates["typecheck"] = tsc_info

    manifest_path = None
    if reference is not None:
        manifest_path = write_render_manifest(
            reference=reference,
            html=html,
            manifest_path=out_dir / "render-manifest.json",
            settings=settings,
            captured=captured,
            console_errors=console_errors,
        )

    summary: dict[str, Any] = {
        "factory": relpath(factory),
        "factorySha256": _sha(factory),
        "factoryFunction": exports.factory_fn,
        "typeName": exports.type_name,
        "passId": pass_id,
        "spec": relpath(spec) if spec else None,
        "reference": relpath(reference) if reference else None,
        "previewHtml": relpath(html),
        "bundleBytes": bundle_info.get("bytes"),
        "hero": hero_cam,
        "viewport": list(settings.viewport),
        "captures": {
            item["id"]: {
                "path": relpath(Path(item["path"])),
                "azimuth": item["azimuth"],
                "elevation": item["elevation"],
                "role": item.get("role"),
            }
            for item in captured
        },
        "comparisonSheet": relpath(comparison) if comparison.is_file() else None,
        "meshes": relpath(meshes_path) if meshes_path.is_file() else None,
        "meshStats": payload.get("meshes"),
        "triangles": (payload.get("state") or {}).get("triangles"),
        "consoleErrors": console_errors,
        "renderManifest": relpath(manifest_path) if manifest_path else None,
        "gates": gates,
        "gatesSummary": summarize_gates(gates),
    }
    write_json(out_dir / "capture.json", summary)
    if history_tag:
        history = out_dir / "history"
        history.mkdir(parents=True, exist_ok=True)
        for name, source_path in (("cmp", comparison), ("hero", capture_paths.get("hero"))):
            if source_path and Path(source_path).is_file():
                shutil.copyfile(source_path, history / f"{history_tag}-{name}.png")
        write_json(history / f"{history_tag}-capture.json", summary)
    log(f"preview: done → {relpath(html)} · sheet {summary['comparisonSheet']}")
    return summary


def _sha(path: Path) -> str:
    return sha256_file(path)
