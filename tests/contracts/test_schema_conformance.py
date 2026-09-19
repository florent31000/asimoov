"""Every dataclass `to_dict()` and every shipped manifest validates against its schema.

`test_schemas.py` validates the frozen examples; this file validates what
the code actually produces (and what `robots/` and `bodies/fake/` ship), so
a dict is valid *before* it is serialized to JSON, not only after.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from asimoov.contracts.app_manifest import AppManifest, AppProvides, AppTrigger
from asimoov.contracts.behaviors import BehaviorManifest
from asimoov.contracts.envelope import Envelope
from asimoov.contracts.face import FaceGaze, FaceState
from asimoov.contracts.fakes import FakeBody
from asimoov.contracts.percepts import (
    AppEvent,
    Battery,
    Bearing,
    BodyState,
    PersonLost,
    PersonSeen,
    SoundEvent,
    SpeechEnded,
    SpeechStarted,
    Touched,
    Utterance,
)
from asimoov.contracts.persona import load_persona

REPO_ROOT = Path(__file__).parents[2]
SCHEMAS_DIR = REPO_ROOT / "src" / "asimoov" / "contracts" / "schemas"
EXAMPLES_DIR = REPO_ROOT / "src" / "asimoov" / "contracts" / "examples"

PERCEPT_INSTANCES = [
    PersonSeen(
        track_id="t1",
        confidence=0.9,
        bearing=Bearing(az=-12.5, el=3.0),
        distance_class="near",
        bbox_norm=(0.1, 0.2, 0.3, 0.4),
        face_quality=0.8,
        person_id="p1",
        name="Sam",
    ),
    PersonSeen(
        track_id="t2",
        confidence=0.52,
        bearing=Bearing(az=8.0),
        distance_class="medium",
        bbox_norm=(0.4, 0.2, 0.6, 0.6),
        face_quality=0.44,
        identity_status="uncertain",
        candidate_person_id="person:sam",
        candidate_name="Sam",
    ),
    PersonLost(track_id="t1", last_bearing=Bearing(az=5.0)),
    SpeechStarted(source="server_vad", direction=Bearing(az=0.0)),
    SpeechEnded(source="server_vad"),
    Utterance(text="bonjour", lang="fr", final=True),
    Touched(where="head", intensity=0.5),
    Battery(level=0.42, charging=True),
    BodyState(posture="standing", moving=False),
    SoundEvent(label="bark", level=0.3),
    AppEvent(name="x.scene_describer.described", payload={"seen": "a table"}),
]


def _validate(schema_name: str, instance: Any) -> None:
    schema = json.loads((SCHEMAS_DIR / schema_name).read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(instance), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{schema_name}: {e.message} at {list(e.path)}" for e in errors)


@pytest.mark.parametrize("percept", PERCEPT_INSTANCES, ids=lambda p: p.PERCEPT_TYPE)
def test_percept_to_dict_validates(percept: Any) -> None:
    _validate("percept.v1.json", percept.to_dict())


def test_envelope_to_dict_validates() -> None:
    _validate(
        "envelope.v1.json",
        Envelope(
            kind="percept",
            topic="percept.person_seen",
            src="perception.face_id",
            data=PERCEPT_INSTANCES[0].to_dict(),
        ).to_dict(),
    )


def test_face_state_to_dict_validates() -> None:
    _validate(
        "face_state.v1.json",
        FaceState(emotion="curious", intensity=0.8, gaze=FaceGaze(x=-0.3, y=0.1), lip=0.4).to_dict(),
    )


def test_body_manifest_to_dict_validates() -> None:
    _validate("body.v1.json", FakeBody().manifest.to_dict())


def test_behavior_manifest_to_dict_validates() -> None:
    manifest = BehaviorManifest(
        name="shake_hand",
        version=1,
        description="Shake hands.",
        duration_class="long",
        requires=("gesture.hand_right",),
        fallback="wave_hello",
    )
    _validate("behavior.v1.json", manifest.to_dict())


def test_persona_to_dict_validates() -> None:
    persona = load_persona(EXAMPLES_DIR / "persona.neon.yaml")
    _validate("persona.v1.json", persona.to_dict())


def test_app_manifest_to_dict_validates() -> None:
    manifest = AppManifest(
        name="scene-describer",
        version="0.1.0",
        description="Describe what the camera sees.",
        entrypoint="asimoov_app_scene_describer:App",
        permissions=("camera.frames",),
        provides=AppProvides(
            tools=("describe_scene",),
            triggers=(AppTrigger(on="utterance", match="décris", hint_tool="describe_scene"),),
        ),
    )
    _validate("app.v1.json", manifest.to_dict())


@pytest.mark.parametrize(
    ("schema_name", "relative_path"),
    [
        ("body.v1.json", "src/asimoov/bodies/fake/manifest.yaml"),
        ("persona.v1.json", "robots/avatar/persona.yaml"),
        ("persona.v1.json", "robots/go2/persona.yaml"),
    ],
)
def test_shipped_yaml_validates(schema_name: str, relative_path: str) -> None:
    instance = yaml.safe_load((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    _validate(schema_name, instance)


def test_fake_body_manifest_matches_its_yaml() -> None:
    shipped = yaml.safe_load(
        (REPO_ROOT / "src/asimoov/bodies/fake/manifest.yaml").read_text(encoding="utf-8")
    )
    assert FakeBody().manifest.to_dict() == shipped
