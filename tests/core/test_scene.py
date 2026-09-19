"""Scene tracking, speaker attribution and attention hysteresis."""

from __future__ import annotations

from asimoov.contracts.percepts import Bearing
from asimoov.core.scene.attention import AttentionPolicy, attribute_speaker
from asimoov.core.scene.scene import Presence, SceneTracker


def seen(track_id: str, az: float, *, confidence: float = 0.9, person_id=None, name=None):
    return {
        "type": "person_seen",
        "track_id": track_id,
        "confidence": confidence,
        "bearing": {"az": az, "el": 0.0},
        "distance_class": "near",
        "bbox_norm": [0.4, 0.3, 0.2, 0.3],
        "face_quality": 0.8,
        "person_id": person_id,
        "name": name,
    }


def presence(track_id: str, az: float, **kwargs) -> Presence:
    return Presence(track_id=track_id, bearing=Bearing(az=az, el=0.0), confidence=0.9, **kwargs)


def test_person_seen_then_lost():
    tracker = SceneTracker()
    assert tracker.apply("person_seen", seen("t1", 10.0), now=100.0)
    scene = tracker.snapshot(100.0)
    assert [person.track_id for person in scene.people] == ["t1"]
    assert scene.attention_track_id == "t1"

    assert tracker.apply("person_lost", {"track_id": "t1", "last_bearing": {"az": 10.0}}, now=101.0)
    assert tracker.snapshot(101.0).people == ()
    assert tracker.snapshot(101.0).attention_track_id is None


def test_identity_survives_a_percept_without_it():
    tracker = SceneTracker()
    tracker.apply("person_seen", seen("t1", 0.0, person_id="person:sam", name="Sam"), now=1.0)
    tracker.apply("person_seen", seen("t1", 2.0), now=2.0)
    person = tracker.snapshot(2.0).person("t1")
    assert (person.person_id, person.name) == ("person:sam", "Sam")


def test_stale_tracks_are_forgotten_on_tick():
    tracker = SceneTracker(forget_after_s=5.0)
    tracker.apply("person_seen", seen("t1", 0.0), now=100.0)
    assert tracker.tick(103.0).people
    assert tracker.tick(120.0).people == ()


def test_speaker_from_direction():
    people = [presence("t1", -30.0), presence("t2", 25.0)]
    assert attribute_speaker(people, direction=Bearing(az=22.0), current_attention=None) == "t2"


def test_direction_too_far_attributes_nobody():
    people = [presence("t1", -80.0), presence("t2", 70.0)]
    assert attribute_speaker(people, direction=Bearing(az=0.0), current_attention="t1") is None


def test_single_person_is_the_speaker():
    assert attribute_speaker([presence("t1", 40.0)], direction=None, current_attention=None) == "t1"


def test_ambiguous_speech_keeps_the_current_attention():
    people = [presence("t1", -20.0), presence("t2", 20.0)]
    assert attribute_speaker(people, direction=None, current_attention="t2") == "t2"
    assert attribute_speaker(people, direction=None, current_attention=None) is None


def test_attention_holds_before_switching():
    policy = AttentionPolicy(hold_s=2.0, margin=0.05)
    people = [presence("t1", 40.0), presence("t2", 0.0)]
    # t2 is better centered, but t1 was chosen 0.5 s ago.
    assert policy.choose(people, current="t1", speaker_track_id=None, now=10.5, chosen_at=10.0) == "t1"
    assert policy.choose(people, current="t1", speaker_track_id=None, now=13.0, chosen_at=10.0) == "t2"


def test_the_speaker_overrides_the_hold():
    policy = AttentionPolicy(hold_s=5.0)
    people = [presence("t1", 0.0), presence("t2", 50.0)]
    assert policy.choose(people, current="t1", speaker_track_id="t2", now=10.1, chosen_at=10.0) == "t2"


def test_attention_does_not_flap_on_a_tiny_advantage():
    policy = AttentionPolicy(hold_s=0.0, margin=0.3)
    people = [presence("t1", 10.0), presence("t2", 8.0)]
    assert policy.choose(people, current="t1", speaker_track_id=None, now=50.0, chosen_at=0.0) == "t1"


def test_speech_percepts_move_the_speaker_flag():
    tracker = SceneTracker()
    tracker.apply("person_seen", seen("t1", 12.0), now=1.0)
    tracker.apply("speech_started", {"type": "speech_started", "source": "voice", "direction": {"az": 10.0}}, now=2.0)
    scene = tracker.snapshot(2.0)
    assert scene.speaker_track_id == "t1"
    assert scene.speech_active is True
    assert scene.person("t1").speaking is True

    tracker.apply("speech_ended", {"type": "speech_ended", "source": "voice"}, now=3.0)
    scene = tracker.snapshot(3.0)
    assert scene.speech_active is False
    assert scene.last_speech_ended_at == 3.0
    assert scene.person("t1").speaking is False


def test_bind_person_attaches_an_identity_to_a_track():
    tracker = SceneTracker()
    tracker.apply("person_seen", seen("t1", 0.0), now=1.0)
    assert tracker.bind_person("t1", "person:sam", "Sam") is True
    assert tracker.snapshot(1.0).person("t1").name == "Sam"
    assert tracker.bind_person("nope", "person:x", "X") is False
