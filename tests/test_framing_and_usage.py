"""Framing calibration and token accounting — the two things that made a real run report
nonsense: a render 33% under the reference's scale, and a token total counted three times."""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

from support import REPO_ROOT, temp_dir  # noqa: E402

from auto3d.codex import CodexResult  # noqa: E402
from auto3d.config import load_settings  # noqa: E402
from auto3d.jobs import create_job  # noqa: E402
from auto3d.pipeline import Pipeline  # noqa: E402
from auto3d.preview import DEFAULT_MARGIN, framing_area_ratio  # noqa: E402
from auto3d.util import SKILL_ROOT  # noqa: E402

sys.path.insert(0, str(SKILL_ROOT / "forge" / "stage4_review"))
from make_comparison_sheet import write_png_rgb  # noqa: E402

PLATE = (242, 242, 242)


def subject_at(path: Path, *, size: int = 256, fill: float = 0.8) -> Path:
    """One blob centred on the pipeline's plate, `fill` of the canvas tall."""
    pixels = [PLATE] * (size * size)
    height = int(size * fill)
    width = max(4, height // 3)
    top = (size - height) // 2
    left = (size - width) // 2
    for y in range(top, top + height):
        for x in range(left, left + width):
            pixels[y * size + x] = (150, 60, 50)
    write_png_rgb(path, size, size, pixels)
    return path


def result(thread: str, **usage: int) -> CodexResult:
    return CodexResult(
        returncode=0,
        thread_id=thread,
        last_message="",
        structured=None,
        usage=usage,
        duration=1.0,
        timed_out=False,
        events_path=None,
    )


class FramingRatioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = temp_dir(prefix="auto3d-framing-")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_same_framing_reads_as_one(self) -> None:
        image = subject_at(self.tmp / "a.png", fill=0.8)
        self.assertAlmostEqual(framing_area_ratio(image, image), 1.0, places=3)

    def test_a_smaller_render_reads_below_one_and_the_correction_closes_it(self) -> None:
        reference = subject_at(self.tmp / "reference.png", fill=0.9)
        render = subject_at(self.tmp / "render.png", fill=0.6)
        ratio = framing_area_ratio(reference, render)
        self.assertIsNotNone(ratio)
        self.assertLess(ratio, 1.0, "a render that fills less of the frame must read below 1")
        # bbox area goes as 1/margin², so this is the correction calibrate_margin applies
        import math

        corrected = DEFAULT_MARGIN * math.sqrt(ratio)
        self.assertLess(corrected, DEFAULT_MARGIN)
        # applying it should predict a render at the reference's scale, within a pixel-grid rounding
        predicted = ratio * (DEFAULT_MARGIN / corrected) ** 2
        self.assertAlmostEqual(predicted, 1.0, places=3)

    def test_unusable_masks_return_none_rather_than_guessing(self) -> None:
        blank = self.tmp / "blank.png"
        write_png_rgb(blank, 64, 64, [PLATE] * (64 * 64))
        self.assertIsNone(framing_area_ratio(blank, blank))


class UsageAccountingTest(unittest.TestCase):
    """Codex reports usage for the whole thread, so a resumed turn repeats what earlier turns
    already reported. Summing those was counting one thread many times over."""

    def setUp(self) -> None:
        self.work_root = REPO_ROOT / "work" / "auto3d-usage-test"
        shutil.rmtree(self.work_root, ignore_errors=True)
        self.settings = load_settings(config_path=None, overrides={"work_root": str(self.work_root)})

    def tearDown(self) -> None:
        shutil.rmtree(self.work_root, ignore_errors=True)

    def _pipeline(self) -> Pipeline:
        return Pipeline(self.settings, create_job(self.settings, "usage", name="usage"))

    def test_cumulative_thread_usage_is_recorded_once(self) -> None:
        pipeline = self._pipeline()
        for total in (1000, 2500, 4000):
            pipeline._record_usage(result("t1", input_tokens=total, cached_input_tokens=total // 2, output_tokens=total // 10))
        usage = pipeline.job.state["usage"]
        self.assertEqual(usage["input_tokens"], 4000, "the last cumulative reading is the job total")
        self.assertEqual(usage["cached_input_tokens"], 2000)
        self.assertEqual(usage["output_tokens"], 400)

    def test_separate_threads_add_up(self) -> None:
        pipeline = self._pipeline()
        pipeline._record_usage(result("prompt", input_tokens=100, output_tokens=10))
        pipeline._record_usage(result("build", input_tokens=900, output_tokens=90))
        pipeline._record_usage(result("build", input_tokens=1500, output_tokens=150))
        usage = pipeline.job.state["usage"]
        self.assertEqual(usage["input_tokens"], 1600)
        self.assertEqual(usage["output_tokens"], 160)

    def test_a_counter_going_backwards_is_taken_as_a_fresh_reading(self) -> None:
        pipeline = self._pipeline()
        pipeline._record_usage(result("t1", input_tokens=5000, output_tokens=500))
        pipeline._record_usage(result("t1", input_tokens=200, output_tokens=20))
        usage = pipeline.job.state["usage"]
        self.assertEqual(usage["input_tokens"], 5200, "non-cumulative reports must still accumulate")

    def test_usage_without_a_thread_id_still_counts(self) -> None:
        pipeline = self._pipeline()
        pipeline._record_usage(
            CodexResult(returncode=0, thread_id=None, last_message="", structured=None, usage={"input_tokens": 42}, duration=0.0, timed_out=False, events_path=None)
        )
        self.assertEqual(pipeline.job.state["usage"]["input_tokens"], 42)


class TurnBudgetTest(unittest.TestCase):
    def test_the_first_build_turn_gets_its_own_timeout(self) -> None:
        work_root = REPO_ROOT / "work" / "auto3d-budget-test"
        shutil.rmtree(work_root, ignore_errors=True)
        try:
            settings = load_settings(
                config_path=None,
                overrides={"work_root": str(work_root), "turn_timeout_min": 60, "first_turn_timeout_min": 120, "job_timeout_min": 600},
            )
            pipeline = Pipeline(settings, create_job(settings, "budget", name="budget"))
            self.assertEqual(pipeline._turn_timeout(), 3600)
            self.assertEqual(pipeline._first_turn_timeout(), 7200)
        finally:
            shutil.rmtree(work_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
