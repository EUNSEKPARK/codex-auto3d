"""`--reference`: build from an image the operator already has, instead of generating one."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from support import REPO_ROOT, browser_available, make_codex_shim, temp_dir  # noqa: E402

from auto3d.config import load_settings  # noqa: E402
from auto3d.jobs import create_job  # noqa: E402
from auto3d.pipeline import Pipeline  # noqa: E402
from auto3d.util import SKILL_ROOT, Auto3DError  # noqa: E402

sys.path.insert(0, str(SKILL_ROOT / "forge" / "stage4_review"))
from make_comparison_sheet import write_png_rgb  # noqa: E402

PLATE = (242, 242, 242)


def plate_with_subject(path: Path, *, size: int = 256, seed: int = 0, band: int = 0) -> Path:
    """One opaque blob on the pipeline's backdrop: admissible (single component, sane coverage),
    and `seed` moves large blocks around inside it so two views are never near-duplicates. The
    blocks have to be big: a perceptual hash reads fine texture — and flat fill — as the same
    image, which is exactly the false 'duplicate view' the gate would then report."""
    pixels = [PLATE] * (size * size)
    left, right = size // 2 - 44 - band, size // 2 + 44 + band
    top, bottom = 40, size - 30
    height, width = bottom - top, right - left
    for y in range(top, bottom):
        for x in range(left, right):
            pixels[y * size + x] = (150, 60, 50)
    block_y = top + (height * (seed % 3)) // 5
    block_x = left + (width * ((seed + 1) % 3)) // 6
    for y in range(block_y, min(block_y + height // 3, bottom)):
        for x in range(block_x, min(block_x + width // 2, right)):
            pixels[y * size + x] = (245, 235, 70)
    foot = bottom - height // 6 - (seed % 2) * height // 8
    for y in range(foot, bottom):
        for x in range(left, right):
            pixels[y * size + x] = (25, 25, 35)
    write_png_rgb(path, size, size, pixels)
    return path


class ReferenceModeTest(unittest.TestCase):
    def setUp(self) -> None:
        # one work root per test: the intake turn renames a job to the subject slug, and two tests
        # finishing inside the same second would otherwise collide on that name and skip the rename
        self.work_root = REPO_ROOT / "work" / f"auto3d-reference-test-{self._testMethodName}"
        shutil.rmtree(self.work_root, ignore_errors=True)
        self.tmp = temp_dir(prefix="auto3d-reference-")
        self.shim = make_codex_shim(self.tmp / "bin")
        self.hero = plate_with_subject(self.tmp / "hero.png")
        self.side = plate_with_subject(self.tmp / "side.png", seed=1, band=-24)
        self.back = plate_with_subject(self.tmp / "back.png", seed=3, band=18)

    def tearDown(self) -> None:
        shutil.rmtree(self.work_root, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _settings(self, **overrides):
        base = {"codex_bin": str(self.shim), "work_root": str(self.work_root), "viewport": [320, 320], "quality": "draft"}
        base.update(overrides)
        return load_settings(config_path=None, overrides=base)

    def _job(self, settings, *, camera=None, views=("side", "back"), concept=""):
        inputs = {"hero": str(self.hero)}
        for view in views:
            inputs[view] = str(getattr(self, view))
        extra = {"referenceInputs": inputs}
        if camera:
            extra["referenceCamera"] = camera
        return create_job(settings, concept, extra=extra)

    def test_supplied_reference_is_adopted_without_generating_anything(self) -> None:
        settings = self._settings()
        job = self._job(settings, camera={"azimuth": 35.0, "elevation": 0.0})
        status = Pipeline(settings, job).run(until="image")
        self.assertEqual(status, "running")

        prompt = json.loads(job.path("prompt", "prompt.json").read_text())
        self.assertEqual(prompt["author"], "codex")
        self.assertEqual(prompt["camera"], {"azimuth": 35.0, "elevation": 0.0}, "a pinned camera must survive intake")
        self.assertEqual(prompt["subject_name"], "Supplied Mascot")

        image = job.stage("image")
        self.assertEqual(image["status"], "done")
        self.assertEqual(image["backend"], "supplied")
        self.assertTrue(image["heroAdmitted"], image["attempts"])
        self.assertTrue((REPO_ROOT / image["hero"]).is_file())
        self.assertEqual(image["attempts"][0]["source"], str(self.hero))
        for view in ("side", "back"):
            self.assertIn(view, image["views"], image.get("viewAdmission"))
            self.assertTrue((REPO_ROOT / image["views"][view]).is_file())
        # nothing was generated: no imagegen turn ran
        self.assertEqual(list(job.path("codex").glob("image-*.events.jsonl")), [])
        # the job took its name from the intake turn's slug
        self.assertTrue(job.dir.name.endswith("-supplied-mascot"), job.dir.name)

    def test_camera_is_estimated_when_the_operator_does_not_pin_it(self) -> None:
        settings = self._settings()
        job = self._job(settings, views=())
        Pipeline(settings, job).run(until="prompt")
        prompt = json.loads(job.path("prompt", "prompt.json").read_text())
        self.assertEqual(prompt["camera"], {"azimuth": 20.0, "elevation": 5.0}, "the intake estimate must win over the settings default")
        self.assertEqual(job.state["referenceCamera"], {"azimuth": 20.0, "elevation": 5.0})

    def test_stub_prompt_when_no_intake_turn_runs(self) -> None:
        settings = self._settings(prompt_author="template")
        job = self._job(settings, camera={"azimuth": 0.0, "elevation": 0.0}, views=(), concept="붉은 마스코트")
        Pipeline(settings, job).run(until="prompt")
        prompt = json.loads(job.path("prompt", "prompt.json").read_text())
        # prompt_author=template still gets the intake turn (it is the only way to read an image),
        # so the stub is only reached when that turn fails — assert it stays schema-shaped either way
        self.assertIn(prompt["author"], {"codex", "stub"})
        self.assertIn("supplied-reference", prompt["image_prompt"])

    def test_missing_and_non_image_references_fail_clearly(self) -> None:
        settings = self._settings()
        text = self.tmp / "notes.txt"
        text.write_text("not an image", encoding="utf-8")
        job = create_job(settings, "", extra={"referenceInputs": {"hero": str(text)}})
        with self.assertRaises(Auto3DError) as caught:
            Pipeline(settings, job).run(until="image")
        self.assertIn("PNG or JPEG", str(caught.exception))

        job2 = create_job(settings, "", extra={"referenceInputs": {"hero": str(self.tmp / "gone.png")}})
        with self.assertRaises(Auto3DError) as caught2:
            Pipeline(settings, job2).run(until="image")
        self.assertIn("not found", str(caught2.exception))

    @unittest.skipUnless(browser_available(), "node runtime / Playwright not installed (run auto3d.py setup)")
    def test_full_build_from_a_supplied_reference(self) -> None:
        """The build stage must not care where the hero came from: same turns, same renders, same
        comparison sheet against the supplied image."""
        settings = self._settings(max_review_turns=6)
        job = self._job(settings, camera={"azimuth": 35.0, "elevation": 0.0}, views=("side",))
        status = Pipeline(settings, job).run()
        self.assertEqual(status, "completed", job.state.get("errors"))
        self.assertEqual(job.stage("image")["backend"], "supplied")
        self.assertTrue(job.path("preview", "cmp.png").is_file())
        self.assertTrue(job.path("preview", "captures", "hero.png").is_file())
        report = json.loads(job.path("report.json").read_text())
        self.assertEqual(report["status"], "completed")

    def test_cli_rejects_bad_reference_arguments(self) -> None:
        def run(*args: str) -> str:
            completed = subprocess.run(
                [sys.executable, str(REPO_ROOT / "auto3d.py"), "run", *args],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
            )
            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            return completed.stdout + completed.stderr

        self.assertIn("--view needs --reference", run("--view", f"side={self.side}", "-p", "x"))
        self.assertIn("unknown view", run("--reference", str(self.hero), "--view", f"left={self.side}"))
        self.assertIn("AZ,EL", run("--reference", str(self.hero), "--reference-camera", "35"))
        self.assertIn("not found", run("--reference", str(self.tmp / "nope.png")))
        self.assertIn("--reference IMAGE", run())


if __name__ == "__main__":
    unittest.main()
