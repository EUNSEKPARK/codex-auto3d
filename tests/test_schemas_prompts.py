from __future__ import annotations

import unittest

from support import INTEGRATION_ROOT  # noqa: E402

from auto3d import prompts, schemas  # noqa: E402
from auto3d.config import PASS_ORDER, QUALITY_TARGET_PASS, Settings, load_settings, validate_settings  # noqa: E402
from auto3d.util import Auto3DError, extract_first_json, pascal_case, slugify  # noqa: E402


class SchemaTest(unittest.TestCase):
    def test_turn_schema_accepts_valid_turn(self) -> None:
        value = {
            "stage": "factory-ready",
            "pass_id": "blockout",
            "factory_path": "work/x/src/createXModel.ts",
            "spec_path": "work/x/object-sculpt-spec.json",
            "factory_function": "createXModel",
            "review": None,
            "state_status": "LOCAL_STATE ...",
            "corrections_used": 0,
            "changed_files": [],
            "message": "ok",
            "message_ko": "확인",
        }
        self.assertEqual(schemas.validate_against(schemas.TURN_SCHEMA, value), [])
        value["review"] = {"pass_id": "blockout", "action": "continue", "fidelity": 0.8, "ai_vision_score": 0.8, "notes": None}
        self.assertEqual(schemas.validate_against(schemas.TURN_SCHEMA, value), [])

    def test_turn_schema_rejects_bad_stage_and_extra_keys(self) -> None:
        value = {"stage": "maybe", "extra": 1}
        problems = schemas.validate_against(schemas.TURN_SCHEMA, value)
        self.assertTrue(any("stage" in p for p in problems))
        self.assertTrue(any("extra" in p for p in problems))

    def test_prompt_schema_roundtrip_with_template(self) -> None:
        data = prompts.template_prompt("빨간 장난감 로봇", style="stylized", hero_camera={"azimuth": 35, "elevation": 15}, views=["front"])
        self.assertEqual(schemas.validate_against(schemas.PROMPT_SCHEMA, data), [])
        self.assertIn("three-quarter", data["image_prompt"])
        self.assertIsNotNone(data["view_prompts"]["front"])
        self.assertIsNone(data["view_prompts"]["back"])


class PromptTemplateTest(unittest.TestCase):
    def test_build_start_mentions_every_fixed_path(self) -> None:
        text = prompts.build_start_turn(
            job_rel="work/auto3d/j1",
            subject_name="Red Toy Robot",
            concept="빨간 로봇",
            profile="character",
            complexity="moderate",
            target_pass="material-pass",
            reference_rel="work/auto3d/j1/reference/hero.png",
            extra_views={"front": "work/auto3d/j1/reference/front.png"},
            camera={"azimuth": 35, "elevation": 15},
            identity_features=["round head"],
            materials=["matte plastic"],
            max_per_pass=3,
            max_total=6,
            target_triangles=60000,
            factory_rel="work/auto3d/j1/src/createRedToyRobotModel.ts",
            language="ko",
        )
        for needle in (
            "forge/state.py init --state work/auto3d/j1/.img2threejs/state.json",
            "--profile character",
            "--character",
            "work/auto3d/j1/object-sculpt-spec.json",
            "createRedToyRobotModel.ts",
            "--yaw 35 --pitch 15",
            "front: work/auto3d/j1/reference/front.png",
            "blockout → structural-pass → form-refinement → material-pass",
            'stage="factory-ready"',
        ):
            self.assertIn(needle, text)

    def test_review_turn_final_flag(self) -> None:
        capture = {
            "captures": {"hero": {"path": "p/hero.png", "azimuth": 35, "elevation": 15}, "hero-mapstripped": {"path": "p/ms.png", "azimuth": 35, "elevation": 15}},
            "comparisonSheet": "p/cmp.png",
            "renderManifest": "p/render-manifest.json",
            "meshes": "p/meshes.json",
            "consoleErrors": [],
        }
        text = prompts.review_turn(job_rel="work/auto3d/j1", pass_id="blockout", target_pass="material-pass", capture=capture, gates_summary="- ok", factory_rel="f.ts", turn_index=2, turns_left=0, corrections_left=6, reference_rel="ref.png", final=True)
        self.assertIn("FINAL REVIEW TURN", text)
        self.assertIn("--map-stripped-render p/ms.png", text)
        self.assertIn("append_review.py work/auto3d/j1/object-sculpt-spec.json --pass-id blockout", text)
        text2 = prompts.review_turn(job_rel="work/auto3d/j1", pass_id="form-refinement", target_pass="material-pass", capture=capture, gates_summary="- ok", factory_rel="f.ts", turn_index=3, turns_left=4, corrections_left=None, reference_rel="ref.png")
        self.assertNotIn("FINAL REVIEW TURN", text2)


class SettingsTest(unittest.TestCase):
    def test_quality_presets_and_pass_ids(self) -> None:
        for preset, target in QUALITY_TARGET_PASS.items():
            self.assertEqual(load_settings(config_path=None, overrides={"quality": preset}).target_pass, target)
        self.assertEqual(load_settings(config_path=None, overrides={"quality": "lighting-pass"}).target_pass, "lighting-pass")
        with self.assertRaises(Auto3DError):
            load_settings(config_path=None, overrides={"quality": "ultra"})

    def test_views_string_is_split_and_validated(self) -> None:
        settings = load_settings(config_path=None, overrides={"views": "front, side"})
        self.assertEqual(settings.views, ["front", "side"])
        with self.assertRaises(Auto3DError):
            load_settings(config_path=None, overrides={"views": ["diagonal"]})

    def test_invalid_backend(self) -> None:
        settings = Settings()
        settings.set("image_backend", "dalle")
        with self.assertRaises(Auto3DError):
            validate_settings(settings)


class UtilTest(unittest.TestCase):
    def test_integration_root_is_on_path(self) -> None:
        self.assertTrue((INTEGRATION_ROOT / "auto3d.py").is_file())

    def test_slug_and_pascal(self) -> None:
        self.assertEqual(slugify("Red Toy Robot!"), "red-toy-robot")
        self.assertEqual(slugify("빨간 로봇"), "subject")
        self.assertEqual(pascal_case("red toy robot"), "RedToyRobot")
        self.assertEqual(pascal_case("Sony WF-1000XM3"), "SonyWF1000XM3")

    def test_extract_json_from_prose_and_fences(self) -> None:
        self.assertEqual(extract_first_json('Here you go:\n```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_first_json('text {"a": {"b": [1, 2]}} trailing'), {"a": {"b": [1, 2]}})
        self.assertIsNone(extract_first_json("no json here"))
        self.assertEqual(PASS_ORDER[0], "blockout")


if __name__ == "__main__":
    unittest.main()
