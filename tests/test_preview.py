from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from support import REPO_ROOT, browser_available, temp_dir  # noqa: E402

from auto3d.config import load_settings  # noqa: E402
from auto3d.preview import build_bundle, capture_plan, detect_exports, render_factory, write_preview_html  # noqa: E402
from auto3d.util import Auto3DError  # noqa: E402

FIXTURE = REPO_ROOT / "forge" / "tests" / "fixtures" / "implicit_character_torso_limb.json"


def _generate_factory(spec: Path, out: Path) -> None:
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "forge" / "stage3_build" / "generate_threejs_factory.py"), str(spec), "--out", str(out), "--allow-nonstrict", "--force"],
        cwd=str(REPO_ROOT),
        check=True,
        capture_output=True,
    )


class DetectExportsTest(unittest.TestCase):
    def test_detects_factory_and_helpers(self) -> None:
        source = "export function createOakModel(options: X = {}): THREE.Group {}\nexport function frameOakCamera() {}"
        exports = detect_exports(source)
        self.assertEqual(exports.factory_fn, "createOakModel")
        self.assertEqual(exports.type_name, "Oak")
        self.assertEqual(exports.frame_fn, "frameOakCamera")

    def test_missing_export_raises(self) -> None:
        with self.assertRaises(Auto3DError):
            detect_exports("const x = 1;")

    def test_capture_plan_has_turntable_and_hero(self) -> None:
        plan = capture_plan({"azimuth": 35, "elevation": 15}, character=True)
        ids = [item["id"] for item in plan]
        for needed in ("hero", "hero-mapstripped", "az000", "az090", "rear", "az270", "orbit-plus35", "head-hero"):
            self.assertIn(needed, ids)
        self.assertTrue(next(item for item in plan if item["id"] == "hero-mapstripped")["mapStripped"])


@unittest.skipUnless(browser_available(), "node runtime / Playwright not installed (run auto3d.py setup)")
class RenderFactoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir()
        self.factory = self.dir / "createImplicitCharacterTorsoLimbModel.ts"
        _generate_factory(FIXTURE, self.factory)
        self.settings = load_settings(config_path=None, overrides={"viewport": [400, 400]})

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_bundle_and_html(self) -> None:
        bundle, info = build_bundle(self.factory, self.dir / "build")
        self.assertGreater(info["bytes"], 500_000)
        exports = detect_exports(self.factory.read_text())
        html = write_preview_html(bundle, {**exports.as_config(), "hero": {"azimuth": 35, "elevation": 15}}, self.dir / "preview.html")
        text = html.read_text()
        self.assertIn("__IMG2THREEJS_CAPTURE__", text)
        self.assertIn("window.__AUTO3D_CONFIG__", text)
        self.assertNotIn("</script>\n</script>", text)

    def test_render_factory_end_to_end(self) -> None:
        # first render doubles as the reference so the gate code paths run
        first = render_factory(factory=self.factory, out_dir=self.dir / "p0", settings=self.settings, reference=None, pass_id="blockout", character=True)
        hero = REPO_ROOT / first["captures"]["hero"]["path"] if not Path(first["captures"]["hero"]["path"]).is_absolute() else Path(first["captures"]["hero"]["path"])
        self.assertTrue(hero.is_file())
        summary = render_factory(factory=self.factory, out_dir=self.dir / "p1", settings=self.settings, reference=hero, spec=FIXTURE, pass_id="blockout", character=True, history_tag="turn-01")
        self.assertTrue(Path(summary["comparisonSheet"]).is_absolute() or (REPO_ROOT / summary["comparisonSheet"]).is_file())
        self.assertEqual(summary["consoleErrors"], [])
        self.assertGreater(summary["triangles"], 0)
        gates = summary["gates"]
        self.assertIn("turntable", gates)
        self.assertTrue(gates["turntable"]["result"]["passed"])
        self.assertIn("selfIntersection", gates)
        self.assertFalse(gates["selfIntersection"]["result"]["selfIntersecting"])
        self.assertIn("tier1", gates)
        self.assertTrue((self.dir / "p1" / "history" / "turn-01-cmp.png").is_file())
        self.assertTrue((self.dir / "p1" / "render-manifest.json").is_file())
        manifest = json.loads((self.dir / "p1" / "render-manifest.json").read_text())
        recorded = [c for c in manifest["captures"] if c["status"] == "recorded"]
        self.assertGreaterEqual(len(recorded), 5)
        self.assertIn("tsc", summary["gatesSummary"])


if __name__ == "__main__":
    unittest.main()
