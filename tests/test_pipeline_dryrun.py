from __future__ import annotations

import json
import shutil
import unittest

from support import REPO_ROOT, browser_available, make_codex_shim, temp_dir  # noqa: E402

from auto3d.config import load_settings  # noqa: E402
from auto3d.jobs import create_job, list_jobs, load_job  # noqa: E402
from auto3d.pipeline import Pipeline  # noqa: E402
from auto3d.util import Auto3DError  # noqa: E402


class PipelineDryRunTest(unittest.TestCase):
    """End-to-end orchestration against the fake codex. The work root lives under the repository
    so the job paths stay repo-relative exactly like a real run (work/ is gitignored)."""

    def setUp(self) -> None:
        self.work_root = REPO_ROOT / "work" / "auto3d-test"
        shutil.rmtree(self.work_root, ignore_errors=True)
        self.tmp = temp_dir()
        self.shim = make_codex_shim(self.tmp / "bin")

    def tearDown(self) -> None:
        shutil.rmtree(self.work_root, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _settings(self, **overrides):
        base = {"codex_bin": str(self.shim), "work_root": str(self.work_root), "viewport": [320, 320], "quality": "draft", "max_review_turns": 6}
        base.update(overrides)
        return load_settings(config_path=None, overrides=base)

    def test_prompt_and_image_stages(self) -> None:
        settings = self._settings(views=["front"])
        job = create_job(settings, "빨간 장난감 로봇")
        self.assertTrue(job.dir.name.endswith("-subject"))
        status = Pipeline(settings, job).run(until="image")
        self.assertEqual(status, "running")
        # job renamed from the English slug the prompt author produced
        self.assertTrue(job.dir.name.endswith("-red-toy-robot"), job.dir.name)
        prompt = json.loads(job.path("prompt", "prompt.json").read_text())
        self.assertEqual(prompt["camera"], {"azimuth": 35.0, "elevation": 15.0})
        self.assertEqual(prompt["author"], "codex")
        image = job.stage("image")
        self.assertEqual(image["status"], "done")
        self.assertTrue((REPO_ROOT / image["hero"]).is_file())
        self.assertTrue(image["heroAdmitted"], image["attempts"])
        self.assertIn("front", image["views"])
        self.assertTrue((REPO_ROOT / image["views"]["front"]).is_file())

    def test_template_prompt_author(self) -> None:
        settings = self._settings(prompt_author="template")
        job = create_job(settings, "wooden toy train", name="train")
        Pipeline(settings, job).run(until="prompt")
        prompt = json.loads(job.path("prompt", "prompt.json").read_text())
        self.assertEqual(prompt["author"], "template")
        self.assertIn("wooden toy train", prompt["image_prompt"])
        self.assertTrue(job.dir.name.endswith("-train"))

    def test_missing_image_fails_cleanly(self) -> None:
        shim = make_codex_shim(self.tmp / "bin-noimage", mode="noimage")
        settings = self._settings(codex_bin=str(shim))
        job = create_job(settings, "a blue mug", name="mug")
        with self.assertRaises(Auto3DError):
            Pipeline(settings, job).run(until="image")
        self.assertEqual(load_job(job.dir).state["status"], "failed")
        self.assertTrue(job.path("report.html").is_file())

    @unittest.skipUnless(browser_available(), "node runtime / Playwright not installed (run auto3d.py setup)")
    def test_full_build_loop_reaches_target_pass(self) -> None:
        settings = self._settings()
        job = create_job(settings, "a red toy robot", name="robot")
        status = Pipeline(settings, job).run()
        self.assertEqual(status, "completed", job.state.get("errors"))
        build = job.stage("build")
        self.assertEqual(build["targetPass"], "form-refinement")
        self.assertEqual(build["progress"]["completedPasses"], ["blockout", "structural-pass", "form-refinement"])
        kinds = [turn["kind"] for turn in build["turns"]]
        self.assertEqual(kinds[0], "start")
        self.assertGreaterEqual(kinds.count("review"), 3)
        self.assertTrue(job.path("preview", "preview.html").is_file())
        self.assertTrue(job.path("preview", "cmp.png").is_file())
        self.assertTrue(job.path("report.html").is_file())
        self.assertTrue((self.work_root / "index.html").is_file())
        report = json.loads(job.path("report.json").read_text())
        self.assertEqual(report["status"], "completed")
        self.assertGreater(report["usage"]["input_tokens"], 0)
        # history keeps one comparison sheet per rendered turn
        sheets = list(job.path("preview", "history").glob("*-cmp.png"))
        self.assertGreaterEqual(len(sheets), 3)
        self.assertEqual(len(list_jobs(self.work_root)), 1)

    @unittest.skipUnless(browser_available(), "node runtime / Playwright not installed (run auto3d.py setup)")
    def test_blocked_review_marks_job_blocked(self) -> None:
        shim = make_codex_shim(self.tmp / "bin-blocked", mode="blocked")
        settings = self._settings(codex_bin=str(shim))
        job = create_job(settings, "a red toy robot", name="robot-blocked")
        status = Pipeline(settings, job).run()
        self.assertEqual(status, "blocked")
        self.assertEqual(job.stage("build")["outcome"], "blocked")

    @unittest.skipUnless(browser_available(), "node runtime / Playwright not installed (run auto3d.py setup)")
    def test_resume_continues_after_interruption(self) -> None:
        settings = self._settings()
        job = create_job(settings, "a red toy robot", name="robot-resume")
        Pipeline(settings, job).run(until="image")
        # simulate a crash after turn 1 by running the build stage with a tiny budget, then resume
        settings_short = self._settings(max_review_turns=1)
        Pipeline(settings_short, job).run(only="build")
        self.assertIn(job.state["status"], {"partial", "blocked"})
        resumed = load_job(job.dir)
        resumed.stage("build")["status"] = "pending"
        resumed.stage("build").pop("outcome", None)
        resumed.save()
        status = Pipeline(self._settings(max_review_turns=6), resumed).run(only="build")
        self.assertEqual(status, "completed")


if __name__ == "__main__":
    unittest.main()
