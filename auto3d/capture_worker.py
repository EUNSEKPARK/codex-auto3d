#!/usr/bin/env python3
"""Playwright capture worker for the auto3d preview page.

Runs in whichever Python has Playwright installed (the integration's .venv by default). It is
intentionally a standalone script with a JSON job on stdin/argv so the stdlib-only orchestrator
never imports Playwright itself.

Job JSON:
{
  "html": "/abs/path/preview.html",
  "viewport": [900, 900], "devicePixelRatio": 1,
  "timeoutMs": 60000,
  "captures": [{"id": "hero", "azimuth": 35, "elevation": 15, "role": "reference-match", "path": "/abs/hero.png"}],
  "meshesOut": "/abs/meshes.json", "maxTriangles": 250000
}
"""

from __future__ import annotations

import json
import pathlib
import sys
import platform
from typing import Any


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: capture_worker.py <job.json>", file=sys.stderr)
        return 2
    job = json.loads(pathlib.Path(argv[0]).read_text(encoding="utf-8"))
    try:
        from playwright.sync_api import Error as PlaywrightError  # type: ignore[import-not-found]
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except ImportError:
        print(json.dumps({"ok": False, "error": "playwright is not installed in this Python; run `auto3d.py setup`"}))
        return 3

    html = pathlib.Path(job["html"]).resolve()
    viewport = job.get("viewport") or [900, 900]
    dpr = float(job.get("devicePixelRatio") or 1)
    timeout_ms = int(job.get("timeoutMs") or 60000)
    captures: list[dict[str, Any]] = job.get("captures") or []
    console_errors: list[str] = []
    page_errors: list[str] = []
    results: list[dict[str, Any]] = []
    meshes_info: dict[str, Any] | None = None
    page_state: dict[str, Any] = {}

    launch_args = ["--ignore-gpu-blocklist", "--allow-file-access-from-files"]
    if platform.system() == "Linux":
        launch_args += ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--no-sandbox"]

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=launch_args)
            context = browser.new_context(viewport={"width": int(viewport[0]), "height": int(viewport[1])}, device_scale_factor=dpr)
            page = context.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            page.goto(html.as_uri() + "?capture=1", wait_until="load", timeout=timeout_ms)
            page.wait_for_function("() => Boolean(window.__IMG2THREEJS_READY__)", timeout=timeout_ms)
            fatal = page.evaluate("() => window.__IMG2THREEJS_ERROR__ || null")
            if fatal:
                print(json.dumps({"ok": False, "error": str(fatal), "consoleErrors": console_errors, "pageErrors": page_errors}))
                browser.close()
                return 4
            for capture in captures:
                spec = {
                    "azimuthDegrees": capture.get("azimuth", 0),
                    "elevationDegrees": capture.get("elevation", 0),
                    "role": capture.get("role", "orbit"),
                    "zoom": capture.get("zoom", 1),
                    "mapStripped": bool(capture.get("mapStripped", False)),
                }
                applied = page.evaluate("async (spec) => window.__IMG2THREEJS_CAPTURE__.setCamera(spec)", spec)
                if not applied or applied.get("ok") is False:
                    raise RuntimeError(f"setCamera failed for {capture.get('id')}: {applied}")
                canvas = page.evaluate("() => { const c = document.querySelector('canvas'); return c ? {width: c.width, height: c.height} : null; }")
                if not canvas or canvas["width"] <= 0:
                    raise RuntimeError("canvas has zero size")
                out = pathlib.Path(capture["path"]).resolve()
                out.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(out), full_page=False)
                results.append(
                    {
                        "id": capture.get("id"),
                        "role": capture.get("role", "orbit"),
                        "azimuth": float(applied.get("azimuth", capture.get("azimuth", 0))),
                        "elevation": float(applied.get("elevation", capture.get("elevation", 0))),
                        "distance": applied.get("distance"),
                        "fov": applied.get("fov"),
                        "path": str(out),
                        "canvas": canvas,
                    }
                )
            page_state = page.evaluate("() => window.__IMG2THREEJS_CAPTURE__.getState ? window.__IMG2THREEJS_CAPTURE__.getState() : {}")
            meshes_out = job.get("meshesOut")
            if meshes_out:
                payload = page.evaluate(
                    "(opts) => window.__IMG2THREEJS_EXPORT_MESHES__ ? window.__IMG2THREEJS_EXPORT_MESHES__(opts) : null",
                    {"maxTriangles": int(job.get("maxTriangles") or 250000)},
                )
                if payload:
                    target = pathlib.Path(meshes_out).resolve()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(json.dumps(payload), encoding="utf-8")
                    meshes_info = {
                        "path": str(target),
                        "meshCount": len(payload.get("meshes", [])),
                        "skipped": payload.get("skipped", []),
                        "triangles": sum(int(m.get("triangles", 0)) for m in payload.get("meshes", [])),
                    }
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - depends on the browser
        print(json.dumps({"ok": False, "error": f"playwright: {exc}", "consoleErrors": console_errors, "pageErrors": page_errors}))
        return 5
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc), "consoleErrors": console_errors, "pageErrors": page_errors, "captures": results}))
        return 6

    print(
        json.dumps(
            {
                "ok": True,
                "captures": results,
                "consoleErrors": console_errors,
                "pageErrors": page_errors,
                "state": page_state,
                "meshes": meshes_info,
                "browser": {"adapter": "playwright", "browser": "chromium", "headless": True, "args": launch_args},
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
