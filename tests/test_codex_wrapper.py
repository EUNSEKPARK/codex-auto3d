from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from support import REPO_ROOT, make_codex_shim, temp_dir  # noqa: E402

from auto3d import schemas  # noqa: E402
from auto3d.codex import build_command, run_codex  # noqa: E402
from auto3d.config import load_settings  # noqa: E402


class CodexWrapperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir()
        self.shim = make_codex_shim(self.dir / "bin")
        self.settings = load_settings(config_path=None, overrides={"codex_bin": str(self.shim)})

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_command_shape_new_and_resume(self) -> None:
        cmd = build_command(self.settings, images=[Path("/tmp/a.png")], output_schema=Path("/tmp/s.json"), last_message=Path("/tmp/o.json"), cwd=REPO_ROOT)
        self.assertEqual(cmd[:2], [str(self.shim), "exec"])
        self.assertIn("--json", cmd)
        self.assertIn("--sandbox", cmd)
        self.assertIn("-C", cmd)
        self.assertIn('approval_policy="never"', cmd)
        self.assertIn('sandbox_mode="workspace-write"', cmd)
        self.assertIn("sandbox_workspace_write.network_access=false", cmd)
        self.assertEqual(cmd[-1], "-")
        resumed = build_command(self.settings, resume_thread="abc", output_schema=Path("/tmp/s.json"), last_message=Path("/tmp/o.json"))
        self.assertEqual(resumed[1:4], ["exec", "resume", "abc"])
        self.assertNotIn("--sandbox", resumed)
        self.assertNotIn("-C", resumed)

    def test_run_parses_events_and_structured_output(self) -> None:
        schema = schemas.write_schema(schemas.PROMPT_SCHEMA, self.dir / "prompt.schema.json")
        result = run_codex(
            self.settings,
            "You are the prompt author for an automated concept → image → 3D pipeline.\n\"\"\"a red toy robot\"\"\"",
            label="t",
            events_path=self.dir / "events.jsonl",
            last_message_path=self.dir / "last.json",
            output_schema=schema,
            sandbox="read-only",
            ephemeral=True,
            timeout_s=60,
        )
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.thread_id)
        self.assertEqual(result.usage.get("output_tokens"), 400)
        self.assertIsInstance(result.structured, dict)
        self.assertEqual(result.structured["subject_slug"], "red-toy-robot")
        self.assertEqual(schemas.validate_against(schemas.PROMPT_SCHEMA, result.structured), [])
        events = [json.loads(line) for line in (self.dir / "events.jsonl").read_text().splitlines() if line.strip()]
        self.assertEqual(events[0]["type"], "thread.started")
        self.assertEqual(events[-1]["type"], "turn.completed")

    def test_prose_answer_is_not_structured(self) -> None:
        shim = make_codex_shim(self.dir / "bin2", mode="nojson")
        settings = load_settings(config_path=None, overrides={"codex_bin": str(shim)})
        result = run_codex(
            settings,
            "You are the prompt author for an automated concept → image → 3D pipeline.",
            label="t2",
            events_path=self.dir / "events2.jsonl",
            last_message_path=self.dir / "last2.json",
            output_schema=None,
            timeout_s=60,
        )
        self.assertTrue(result.ok)
        self.assertIsNone(result.structured)
        self.assertIn("prose", result.last_message)

    def test_timeout_is_reported(self) -> None:
        shim = make_codex_shim(self.dir / "bin3", mode="slow")
        settings = load_settings(config_path=None, overrides={"codex_bin": str(shim)})
        result = run_codex(
            settings,
            "You are running the img2threejs skill UNATTENDED inside an automated pipeline.\n- Job directory: /nonexistent/\n",
            label="t3",
            events_path=self.dir / "events3.jsonl",
            last_message_path=self.dir / "last3.json",
            timeout_s=2,
        )
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
