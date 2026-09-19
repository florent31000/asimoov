"""What ``robots/inmoov/`` ships must validate against the frozen schemas."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from asimoov.bodies.inmoov.adapter import GESTURES_DIR
from asimoov.bodies.inmoov.gestures import load_gestures
from asimoov.contracts.behaviors import BehaviorManifest
from asimoov.contracts.persona import load_persona

REPO_ROOT = Path(__file__).parents[3]
ROBOT_DIR = REPO_ROOT / "robots" / "inmoov"
SCHEMAS_DIR = REPO_ROOT / "src" / "asimoov" / "contracts" / "schemas"


def _validator(schema_name: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((SCHEMAS_DIR / schema_name).read_text("utf-8")))


def test_persona_loads() -> None:
    persona = load_persona(ROBOT_DIR / "persona.yaml")
    assert persona.language == "fr"
    _validator("persona.v1.json").validate(persona.to_dict())


@pytest.mark.parametrize("path", sorted((ROBOT_DIR / "behaviors").glob("*.yaml")))
def test_behaviors_validate(path: Path) -> None:
    payload = yaml.safe_load(path.read_text("utf-8"))
    _validator("behavior.v1.json").validate(payload)
    BehaviorManifest.from_dict(payload)


def test_every_behavior_has_a_gesture() -> None:
    behaviors = {
        yaml.safe_load(path.read_text("utf-8"))["name"]
        for path in (ROBOT_DIR / "behaviors").glob("*.yaml")
    }
    assert behaviors == set(load_gestures(GESTURES_DIR))


def test_robot_yaml_points_at_this_body() -> None:
    payload = yaml.safe_load((ROBOT_DIR / "robot.yaml").read_text("utf-8"))
    assert payload["body"]["type"] == "inmoov"
    assert payload["body"]["link"] in ("serial", "tcp")
