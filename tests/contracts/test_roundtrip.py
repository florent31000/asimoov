"""to_dict/from_dict round-trips for every schema-backed dataclass."""

from __future__ import annotations

import pytest

from asimoov.contracts.app_manifest import (
    AppDegraded,
    AppManifest,
    AppProvides,
    AppRequires,
    AppTrigger,
)
from asimoov.contracts.behaviors import BehaviorCall, BehaviorManifest, BehaviorResult
from asimoov.contracts.body import BodyHealth, BodyManifest
from asimoov.contracts.envelope import Envelope
from asimoov.contracts.face import FaceGaze, FaceState
from asimoov.contracts.memory import Episode, Fact, JournalEntry, Person
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
from asimoov.contracts.persona import Initiative, Persona, Relationships, VoiceConfig
from asimoov.contracts.tools import ToolResult, ToolSpec


def test_envelope_roundtrip() -> None:
    original = Envelope(kind="percept", topic="percept.touched", src="body.avatar", data={"where": "head"})
    again = Envelope.from_dict(original.to_dict())
    assert again == original


def test_person_seen_roundtrip() -> None:
    original = PersonSeen(
        track_id="t1",
        confidence=0.9,
        bearing=Bearing(az=1.0, el=2.0),
        distance_class="near",
        bbox_norm=(0.1, 0.2, 0.3, 0.4),
        face_quality=0.7,
        person_id="p1",
        name="Sam",
    )
    assert PersonSeen.from_dict(original.to_dict()) == original


def test_person_seen_identity_status_defaults_to_the_v1_1_meaning() -> None:
    identified = PersonSeen(
        track_id="t1",
        confidence=0.9,
        bearing=Bearing(az=1.0),
        distance_class="near",
        bbox_norm=(0.1, 0.2, 0.3, 0.4),
        face_quality=0.7,
        person_id="p1",
        name="Sam",
    )
    anonymous = PersonSeen(
        track_id="t2",
        confidence=0.4,
        bearing=Bearing(az=1.0),
        distance_class="far",
        bbox_norm=(0.1, 0.2, 0.3, 0.4),
        face_quality=0.2,
    )
    assert identified.identity_status == "identified"
    assert anonymous.identity_status == "unknown"


def test_person_seen_uncertain_candidate_roundtrips() -> None:
    original = PersonSeen(
        track_id="t3",
        confidence=0.52,
        bearing=Bearing(az=8.0),
        distance_class="medium",
        bbox_norm=(0.4, 0.2, 0.6, 0.6),
        face_quality=0.44,
        identity_status="uncertain",
        candidate_person_id="person:sam",
        candidate_name="Sam",
    )
    payload = original.to_dict()
    assert payload["person_id"] is None
    assert payload["candidate_name"] == "Sam"
    assert PersonSeen.from_dict(payload) == original


def test_person_seen_rejects_an_unknown_identity_status() -> None:
    with pytest.raises(ValueError, match="identity_status"):
        PersonSeen(
            track_id="t4",
            confidence=0.5,
            bearing=Bearing(az=0.0),
            distance_class="near",
            bbox_norm=(0.1, 0.2, 0.3, 0.4),
            face_quality=0.5,
            identity_status="maybe",
        )


def test_person_lost_roundtrip() -> None:
    original = PersonLost(track_id="t1", last_bearing=Bearing(az=5.0), person_id="p1")
    assert PersonLost.from_dict(original.to_dict()) == original


def test_speech_started_ended_roundtrip() -> None:
    started = SpeechStarted(source="server_vad", direction=Bearing(az=0.0))
    assert SpeechStarted.from_dict(started.to_dict()) == started
    ended = SpeechEnded(source="server_vad")
    assert SpeechEnded.from_dict(ended.to_dict()) == ended


def test_utterance_roundtrip() -> None:
    original = Utterance(text="bonjour", lang="fr", final=True, speaker_track_id="t1")
    assert Utterance.from_dict(original.to_dict()) == original


def test_touched_roundtrip() -> None:
    original = Touched(where="head", intensity=0.5)
    assert Touched.from_dict(original.to_dict()) == original


def test_battery_body_state_sound_event_roundtrip() -> None:
    battery = Battery(level=0.5, charging=True)
    assert Battery.from_dict(battery.to_dict()) == battery
    body_state = BodyState(posture="standing", moving=True)
    assert BodyState.from_dict(body_state.to_dict()) == body_state
    sound = SoundEvent(label="bark", level=0.3)
    assert SoundEvent.from_dict(sound.to_dict()) == sound


def test_app_event_roundtrip() -> None:
    original = AppEvent(name="scene_describer.described", payload={"seen": "a table"})
    assert AppEvent.from_dict(original.to_dict()) == original


def test_behavior_manifest_roundtrip() -> None:
    original = BehaviorManifest(
        name="shake_hand",
        version=1,
        description="Shake hands.",
        duration_class="long",
        requires=("gesture.hand_right",),
        fallback="wave_hello",
    )
    assert BehaviorManifest.from_dict(original.to_dict()) == original


def test_behavior_call_roundtrip() -> None:
    original = BehaviorCall(name="shake_hand", params={"person": "current"})
    assert BehaviorCall.from_dict(original.to_dict()) == original


def test_behavior_result_roundtrip() -> None:
    for result in (
        BehaviorResult.ok(samples=5),
        BehaviorResult.started(action_id="a1", eta_s=4.0),
        BehaviorResult.error("no such gesture"),
        BehaviorResult.timeout(),
        BehaviorResult.unsupported(),
    ):
        assert BehaviorResult.from_dict(result.to_dict()) == result


def test_body_manifest_roundtrip() -> None:
    original = BodyManifest(
        name="go2",
        kind_of_body="quadruped",
        capabilities=("locomotion.planar", "gesture.wave", "camera.front", "battery"),
        implements={"wave_hello": {"primitive": "sport", "arg": "Hello", "est_ms": 4000}},
        limits={"max_speed": 0.6},
        safety={"watchdog_ms": 500, "forbidden": ["FrontFlip"]},
        sensors=("battery",),
    )
    assert BodyManifest.from_dict(original.to_dict()) == original


def test_body_health_roundtrip() -> None:
    original = BodyHealth(connected=True, battery=0.8, last_rtt_ms=12.5, errors=("timeout",))
    assert BodyHealth.from_dict(original.to_dict()) == original


def test_face_state_roundtrip() -> None:
    original = FaceState(emotion="curious", intensity=0.8, gaze=FaceGaze(x=-0.3, y=0.1), lip=0.4, blink=True)
    assert FaceState.from_dict(original.to_dict()) == original


def test_persona_roundtrip() -> None:
    original = Persona(
        name="Neon",
        language="fr",
        identity="Tu es {name}.",
        traits=("sympa", "joueur"),
        relationships=Relationships(owners=("Sam",), owners_style="affectueux"),
        initiative=Initiative(level="high", quiet_hours=("22:00", "08:00")),
        rules=("Ne fais jamais de saut.",),
        voice=VoiceConfig(provider="openai_realtime", model="gpt-realtime-1.5", voice="alloy", transcription_language="fr"),
    )
    assert Persona.from_dict(original.to_dict()) == original


def test_app_manifest_roundtrip() -> None:
    original = AppManifest(
        name="scene-describer",
        version="0.1.0",
        description="Describe what the camera sees.",
        entrypoint="asimoov_app_scene_describer:App",
        permissions=("camera.frames", "llm.vision"),
        provides=AppProvides(
            tools=("describe_scene",),
            percepts=("x.scene_describer.described",),
            triggers=(AppTrigger(on="utterance", match="décris", hint_tool="describe_scene"),),
        ),
        requires=AppRequires(capabilities=("camera.front",)),
        degraded=(AppDegraded(when_missing="camera.front", disable=("describe_scene",)),),
    )
    assert AppManifest.from_dict(original.to_dict()) == original


def test_tool_spec_and_result_roundtrip() -> None:
    spec = ToolSpec(name="gesture", description="Run a gesture.", timeout_s=2.0, duration_class="short")
    assert ToolSpec.from_dict(spec.to_dict()) == spec

    for result in (
        ToolResult.from_behavior_result(BehaviorResult.ok(samples=3)),
        ToolResult.from_behavior_result(BehaviorResult.started(action_id="a1", eta_s=2.0)),
        ToolResult.from_behavior_result(BehaviorResult.error("boom")),
    ):
        assert ToolResult.from_dict(result.to_dict()) == result


def test_memory_dataclasses_roundtrip() -> None:
    person = Person(id="p1", name="Sam", relationship="son")
    assert Person.from_dict(person.to_dict()) == person

    fact = Fact(person_id="p1", text="loves dinosaurs")
    assert Fact.from_dict(fact.to_dict()) == fact

    episode = Episode(id="e1", started_at=1.0, ended_at=2.0, participants=("p1",), summary="said hi")
    assert Episode.from_dict(episode.to_dict()) == episode

    entry = JournalEntry(ts=1.0, text="Sam arrived", kind="event")
    assert JournalEntry.from_dict(entry.to_dict()) == entry
