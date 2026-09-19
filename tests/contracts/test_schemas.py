"""Every example in contracts/examples/ validates against its JSON Schema."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

SCHEMAS_DIR = Path(__file__).parents[2] / "src" / "asimoov" / "contracts" / "schemas"
EXAMPLES_DIR = Path(__file__).parents[2] / "src" / "asimoov" / "contracts" / "examples"

SCHEMA_TO_EXAMPLES = {
    "envelope.v1.json": ["envelope.person_seen.json"],
    "percept.v1.json": [
        "percept.person_seen.json",
        "percept.person_seen.uncertain.json",
    ],
    "behavior.v1.json": ["behavior.shake_hand.yaml"],
    "body.v1.json": ["body.go2.yaml"],
    "persona.v1.json": ["persona.neon.yaml"],
    "app.v1.json": ["app.scene-describer.yaml"],
    "face_state.v1.json": ["face_state.curious.json"],
}


def _load(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def test_every_schema_file_is_covered() -> None:
    schema_files = {p.name for p in SCHEMAS_DIR.glob("*.json")}
    assert schema_files == set(SCHEMA_TO_EXAMPLES), (
        "SCHEMA_TO_EXAMPLES in this test must list exactly the schemas in "
        f"{SCHEMAS_DIR}: missing/extra = {schema_files ^ set(SCHEMA_TO_EXAMPLES)}"
    )


@pytest.mark.parametrize(
    ("schema_name", "example_name"),
    [
        (schema_name, example_name)
        for schema_name, examples in SCHEMA_TO_EXAMPLES.items()
        for example_name in examples
    ],
)
def test_example_validates_against_schema(schema_name: str, example_name: str) -> None:
    schema = json.loads((SCHEMAS_DIR / schema_name).read_text(encoding="utf-8"))
    instance = _load(EXAMPLES_DIR / example_name)

    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(instance), key=lambda e: e.path)
    assert not errors, "\n".join(f"{example_name}: {e.message} at {list(e.path)}" for e in errors)
