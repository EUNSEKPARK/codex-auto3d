"""JSON Schemas handed to `codex exec --output-schema` so every Codex stage ends in a machine-
readable message. They are written in the strict structured-output dialect (every property
required, `additionalProperties: false`, nullability via type unions) so they work whether Codex
enforces the schema server-side or merely prompts with it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import PASS_ORDER


def _obj(properties: dict[str, Any], *, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": False,
    }


def _str(description: str = "", *, nullable: bool = False, enum: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": ["string", "null"] if nullable else "string"}
    if description:
        schema["description"] = description
    if enum:
        schema["enum"] = enum + ([None] if nullable else [])
    return schema


def _num(description: str = "", *, nullable: bool = False) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": ["number", "null"] if nullable else "number"}
    if description:
        schema["description"] = description
    return schema


def _int(description: str = "", *, nullable: bool = False) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": ["integer", "null"] if nullable else "integer"}
    if description:
        schema["description"] = description
    return schema


def _arr(items: dict[str, Any], description: str = "") -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "array", "items": items}
    if description:
        schema["description"] = description
    return schema


PROMPT_SCHEMA: dict[str, Any] = _obj(
    {
        "subject_name": _str("Short English display name of the subject, e.g. 'Red Toy Robot'"),
        "subject_slug": _str("kebab-case ASCII slug derived from subject_name"),
        "profile": _str("img2threejs profile", enum=["generic", "character"]),
        "complexity": _str("expected reconstruction complexity", enum=["simple", "moderate", "complex", "ultra-complex"]),
        "image_prompt": _str("Complete labeled image-generation prompt for the hero reference (English)"),
        "view_prompts": _obj(
            {
                "front": _str(nullable=True),
                "side": _str(nullable=True),
                "back": _str(nullable=True),
                "top": _str(nullable=True),
            }
        ),
        "camera": _obj({"azimuth": _num(), "elevation": _num()}),
        "identity_features": _arr(_str(), "3-8 identity-defining features the 3D model must reproduce"),
        "materials": _arr(_str(), "distinct materials named in PBR terms"),
        "avoid": _arr(_str(), "negative constraints applied to the image prompt"),
        "notes_ko": _str("1-3 sentence Korean note explaining the prompt decisions"),
    }
)


IMAGEGEN_SCHEMA: dict[str, Any] = _obj(
    {
        "saved_path": _str("Workspace-relative path of the saved PNG, exactly as requested", nullable=True),
        "generated": {"type": "boolean"},
        "model": _str(nullable=True),
        "size": _str(nullable=True),
        "prompt_used": _str("The final prompt that was sent to the image tool"),
        "notes": _str("Anything that went wrong or was adjusted", nullable=True),
    }
)


REVIEW_ENTRY: dict[str, Any] = _obj(
    {
        "pass_id": _str(enum=PASS_ORDER),
        "action": _str(enum=["continue", "refine-spec", "refine-code", "request-input", "stop"]),
        "fidelity": _num("global fidelity 0-1 as recorded with append_review.py", nullable=True),
        "ai_vision_score": _num(nullable=True),
        "notes": _str(nullable=True),
    }
)


TURN_SCHEMA: dict[str, Any] = _obj(
    {
        "stage": _str(
            "factory-ready: a factory was (re)generated or edited and must be rendered now; "
            "done: the target pass has a review with action=continue; "
            "blocked: cannot proceed (hard stop, unsupported subject, budget); "
            "failed: an unrecoverable tool/script error",
            enum=["factory-ready", "done", "blocked", "failed"],
        ),
        "pass_id": _str("build pass the factory currently represents", enum=PASS_ORDER, nullable=True),
        "factory_path": _str("workspace-relative path of the TypeScript factory", nullable=True),
        "spec_path": _str("workspace-relative path of the ObjectSculptSpec JSON", nullable=True),
        "factory_function": _str("exported create<Name>Model function name", nullable=True),
        "review": {"anyOf": [REVIEW_ENTRY, {"type": "null"}]},
        "state_status": _str("status line printed by forge/next.py", nullable=True),
        "corrections_used": _int("total refine-spec/refine-code decisions so far", nullable=True),
        "changed_files": _arr(_str(), "workspace-relative files written or edited this turn"),
        "message": _str("concise English progress note"),
        "message_ko": _str("same note in Korean"),
    }
)


def write_schema(schema: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def validate_against(schema: dict[str, Any], value: Any, path: str = "$") -> list[str]:
    """Tiny structural validator (enough for our own schemas: object/array/enum/type unions).
    Returns a list of problems; empty means the value conforms."""
    problems: list[str] = []
    if "anyOf" in schema:
        candidates = schema["anyOf"]
        if not any(not validate_against(candidate, value, path) for candidate in candidates):
            problems.append(f"{path}: matches none of the allowed shapes")
        return problems
    expected = schema.get("type")
    types = expected if isinstance(expected, list) else [expected] if expected else []
    if types and not _type_matches(types, value):
        problems.append(f"{path}: expected {expected}, got {type(value).__name__}")
        return problems
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, dict) and "properties" in schema:
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{path}.{key}: missing")
        for key, child in value.items():
            if key in schema["properties"]:
                problems.extend(validate_against(schema["properties"][key], child, f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                problems.append(f"{path}.{key}: unexpected property")
    if isinstance(value, list) and "items" in schema:
        for index, child in enumerate(value):
            problems.extend(validate_against(schema["items"], child, f"{path}[{index}]"))
    return problems


def _type_matches(types: list[str], value: Any) -> bool:
    for name in types:
        if name == "null" and value is None:
            return True
        if name == "string" and isinstance(value, str):
            return True
        if name == "boolean" and isinstance(value, bool):
            return True
        if name == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if name == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if name == "object" and isinstance(value, dict):
            return True
        if name == "array" and isinstance(value, list):
            return True
    return False
