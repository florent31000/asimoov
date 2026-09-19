"""The face_id pipeline end to end, with stub models and synthetic frames.

Also the envelope/percept conformance check: everything this module publishes
must validate against `percept.v1.json` inside `envelope.v1.json`.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from helpers import (
    RecordingBus,
    ScriptedCamera,
    StubDetection,
    StubDetector,
    StubEmbedder,
    StubStore,
)

from asimoov.contracts.memory import Person
from asimoov.contracts.perception import PerceptionContext
from asimoov.perception.face_id.gallery import (
    IDENTIFIED_THRESHOLD,
    UNCERTAIN_THRESHOLD,
)
from asimoov.perception.face_id.module import (
    PERSON_LOST_TOPIC,
    PERSON_SEEN_TOPIC,
    RELOAD_GALLERY_COMMAND,
    FaceIdModule,
)
from asimoov.perception.models import EMBEDDING_MODEL_ID

KPS = np.array([[10, 10], [30, 10], [20, 20], [12, 30], [28, 30]], dtype=np.float32)


def face(x0=200.0, y0=80.0, x1=280.0, y1=200.0, score=0.95) -> StubDetection:
    return StubDetection(bbox=(x0, y0, x1, y1), score=score, kps=KPS)


def build(script, *, store=None, frames=None, **kwargs):
    camera = ScriptedCamera(frames if frames is not None else len(script))
    module = FaceIdModule(
        camera,
        store=store or StubStore(),
        detector=StubDetector(script),
        embedder=StubEmbedder(),
        **kwargs,
    )
    module._embed_sync = lambda image, track: (StubEmbedder().vector, 0.8)  # noqa: SLF001
    return module, camera


async def run(module: FaceIdModule, bus: RecordingBus) -> None:
    await module.start(PerceptionContext(publish=bus.publish, bus=bus))
    for _ in range(200):
        if module._task.done():  # noqa: SLF001
            break
        await asyncio.sleep(0.005)
    await module.stop()


async def test_a_visible_face_publishes_person_seen_every_frame():
    bus = RecordingBus()
    module, _ = build([[face()], [face()], [face()]])
    await run(module, bus)
    assert bus.topics() == [PERSON_SEEN_TOPIC] * 3


async def test_the_percepts_conform_to_the_schemas(percept_validator, envelope_validator):
    bus = RecordingBus()
    module, _ = build([[face()]] + [[]] * 6)
    await run(module, bus)
    assert PERSON_LOST_TOPIC in bus.topics()
    for envelope in bus.envelopes:
        envelope_validator.validate(envelope.to_dict())
        percept_validator.validate(envelope.data)
        assert envelope.kind == "percept"
        assert envelope.src == "perception.face_id"


async def test_the_bearing_follows_the_frozen_sign_convention():
    bus = RecordingBus()
    module, _ = build([[face(20, 80, 100, 200)]])  # left of a 640-wide frame
    await run(module, bus)
    assert bus.envelopes[0].data["bearing"]["az"] > 0

    bus = RecordingBus()
    module, _ = build([[face(540, 80, 620, 200)]])
    await run(module, bus)
    assert bus.envelopes[0].data["bearing"]["az"] < 0


async def test_the_bbox_is_normalized_and_clipped():
    bus = RecordingBus()
    module, _ = build([[face(-20, -10, 700, 400)]])
    await run(module, bus)
    assert bus.envelopes[0].data["bbox_norm"] == [0.0, 0.0, 1.0, 1.0]


async def test_person_lost_is_published_after_the_hysteresis():
    bus = RecordingBus()
    module, _ = build([[face()]] + [[]] * 6)
    await run(module, bus)
    lost = bus.of_topic(PERSON_LOST_TOPIC)
    assert len(lost) == 1
    assert lost[0].data["track_id"] == "t1"
    assert "last_bearing" in lost[0].data


async def test_an_unknown_face_carries_no_person_id():
    bus = RecordingBus()
    module, _ = build([[face()]], store=StubStore(match=None))
    await run(module, bus)
    assert bus.envelopes[0].data["person_id"] is None
    assert bus.envelopes[0].data["name"] is None


async def test_an_uncertain_match_is_not_reported_as_an_identity():
    bus = RecordingBus()
    module, _ = build([[face()]] * 3, store=StubStore(match=("p_sam", 0.52)))
    await run(module, bus)
    assert all(envelope.data["person_id"] is None for envelope in bus.envelopes)


async def test_a_confident_match_names_the_person():
    store = StubStore(match=("p_sam", 0.82))
    store.persons["p_sam"] = Person(id="p_sam", name="Sam")
    bus = RecordingBus()
    module, _ = build([[face()]] * 3, store=store)
    await run(module, bus)
    last = bus.envelopes[-1].data
    assert last["person_id"] == "p_sam"
    assert last["name"] == "Sam"
    assert last["confidence"] == pytest.approx(0.82)


async def test_two_faces_produce_two_tracks():
    bus = RecordingBus()
    module, _ = build([[face(50, 80, 130, 200), face(400, 80, 480, 200)]])
    await run(module, bus)
    assert {envelope.data["track_id"] for envelope in bus.envelopes} == {"t1", "t2"}


async def test_no_frame_is_ever_published():
    bus = RecordingBus()
    module, _ = build([[face()]] * 3)
    await run(module, bus)
    assert all(not envelope.topic.startswith("frame.") for envelope in bus.envelopes)
    assert all("image" not in envelope.data for envelope in bus.envelopes)


async def test_the_camera_is_released_on_stop():
    bus = RecordingBus()
    module, camera = build([[face()]])
    await run(module, bus)
    assert camera.stopped is True


async def test_enrolling_an_unknown_track_fails_without_hanging():
    module, _ = build([[face()]])
    reply = await module.handle_command("face_id.enroll", {"track_id": "t9", "person_id": "p_sam"})
    assert reply == {"ok": False, "reason": "unknown track_id", "track_id": "t9"}


async def test_an_unknown_command_raises_key_error():
    module, _ = build([[face()]])
    with pytest.raises(KeyError):
        await module.handle_command("face_id.nope", {})


async def test_enrolment_collects_five_samples_and_writes_them():
    store = StubStore()
    bus = RecordingBus()
    module, _ = build([[face()]] * 40, frames=40, store=store, enroll_timeout_s=5.0)
    await module.start(PerceptionContext(publish=bus.publish, bus=bus))
    for _ in range(200):
        if module.tracker.tracks:
            break
        await asyncio.sleep(0.005)
    reply = await module.handle_command(
        "face_id.enroll", {"track_id": "t1", "person_id": "p_sam", "name": "Sam"}
    )
    await module.stop()
    assert reply["ok"] is True
    assert reply["samples"] == 5
    assert len(store.embeddings) == 5
    assert store.persons["p_sam"].name == "Sam"


async def test_enrolment_times_out_when_the_face_is_too_poor():
    store = StubStore()
    bus = RecordingBus()
    module, _ = build([[face()]] * 40, frames=40, store=store, enroll_timeout_s=0.3)
    module._embed_sync = lambda image, track: (StubEmbedder().vector, 0.2)  # noqa: SLF001
    await module.start(PerceptionContext(publish=bus.publish, bus=bus))
    for _ in range(200):
        if module.tracker.tracks:
            break
        await asyncio.sleep(0.005)
    reply = await module.handle_command(
        "face_id.enroll", {"track_id": "t1", "person_id": "p_sam"}
    )
    await module.stop()
    assert reply["ok"] is False
    assert reply["reason"] == "timeout"
    assert store.embeddings == []


async def test_enrolment_requires_both_identifiers():
    module, _ = build([[face()]])
    reply = await module.handle_command("face_id.enroll", {"track_id": "t1"})
    assert reply["ok"] is False
    assert "required" in reply["reason"]


async def track_one(store, **kwargs) -> FaceIdModule:
    """A module whose camera keeps feeding one face, with ``t1`` already live."""
    bus = RecordingBus()
    module, _ = build([[face()]], frames=1000, store=store, **kwargs)
    await module.start(PerceptionContext(publish=bus.publish, bus=bus))
    for _ in range(400):
        if module.tracker.tracks:
            break
        await asyncio.sleep(0.005)
    return module


async def test_an_uncertain_match_is_published_as_a_candidate(percept_validator):
    store = StubStore(match=("p_sam", 0.52))
    store.persons["p_sam"] = Person(id="p_sam", name="Sam")
    bus = RecordingBus()
    module, _ = build([[face()]] * 3, store=store)
    await run(module, bus)
    last = bus.envelopes[-1].data
    percept_validator.validate(last)
    assert last["identity_status"] == "uncertain"
    assert last["person_id"] is None
    assert last["name"] is None
    assert last["candidate_person_id"] == "p_sam"
    assert last["candidate_name"] == "Sam"


async def test_an_identified_match_carries_no_candidate():
    store = StubStore(match=("p_sam", 0.82))
    store.persons["p_sam"] = Person(id="p_sam", name="Sam")
    bus = RecordingBus()
    module, _ = build([[face()]] * 3, store=store)
    await run(module, bus)
    last = bus.envelopes[-1].data
    assert last["identity_status"] == "identified"
    assert last["candidate_person_id"] is None
    assert last["candidate_name"] is None


async def test_an_unknown_face_carries_no_candidate():
    bus = RecordingBus()
    module, _ = build([[face()]], store=StubStore(match=None))
    await run(module, bus)
    assert bus.envelopes[0].data["identity_status"] == "unknown"
    assert bus.envelopes[0].data["candidate_person_id"] is None


@pytest.mark.parametrize(
    ("score", "status"),
    [
        (UNCERTAIN_THRESHOLD - 0.01, "unknown"),
        (UNCERTAIN_THRESHOLD, "uncertain"),
        (IDENTIFIED_THRESHOLD - 0.01, "uncertain"),
        (IDENTIFIED_THRESHOLD, "identified"),
    ],
)
async def test_the_identity_bands_follow_the_gallery_thresholds(score, status):
    store = StubStore(match=("p_sam", score))
    store.persons["p_sam"] = Person(id="p_sam", name="Sam")
    bus = RecordingBus()
    module, _ = build([[face()]] * 3, store=store)
    await run(module, bus)
    assert bus.envelopes[-1].data["identity_status"] == status


async def test_reload_gallery_replies_with_the_identity_count():
    store = StubStore()
    store.embeddings.append(("p_sam", EMBEDDING_MODEL_ID, None, 0.8))
    store.embeddings.append(("p_ana", EMBEDDING_MODEL_ID, None, 0.8))
    module, _ = build([[face()]])
    assert await module.handle_command(RELOAD_GALLERY_COMMAND, {}) == {"ok": True, "identities": 0}
    module.store = store
    assert await module.handle_command(RELOAD_GALLERY_COMMAND, {}) == {"ok": True, "identities": 2}
    assert store.reloads == 1


async def test_reload_gallery_forgets_the_identity_of_a_visible_track():
    store = StubStore(match=("p_sam", 0.9))
    store.persons["p_sam"] = Person(id="p_sam", name="Sam")
    module = await track_one(store)
    try:
        for _ in range(400):
            if module.tracker.tracks["t1"].person_id is not None:
                break
            await asyncio.sleep(0.005)
        assert module.tracker.tracks["t1"].person_id == "p_sam"
        store.match = None
        reply = await module.handle_command(RELOAD_GALLERY_COMMAND, {})
        assert reply == {"ok": True, "identities": 0}
        assert module.tracker.tracks["t1"].person_id is None
        assert module._names == {}  # noqa: SLF001
    finally:
        await module.stop()


async def test_enrolment_preserves_the_relationship_and_the_notes():
    store = StubStore()
    store.persons["p_sam"] = Person(
        id="p_sam", name="Sam", created_at=1.0, relationship="friend", notes="likes tea"
    )
    module = await track_one(store, enroll_timeout_s=5.0)
    try:
        reply = await module.handle_command(
            "face_id.enroll", {"track_id": "t1", "person_id": "p_sam", "name": "Sam"}
        )
    finally:
        await module.stop()
    assert reply["ok"] is True
    assert store.persons["p_sam"].relationship == "friend"
    assert store.persons["p_sam"].notes == "likes tea"


async def test_enrolling_an_existing_person_adds_to_their_gallery():
    store = StubStore()
    store.persons["p_sam"] = Person(id="p_sam", name="Sam", created_at=1.0)
    store.embeddings.append(("p_sam", EMBEDDING_MODEL_ID, None, 0.7))
    module = await track_one(store, enroll_timeout_s=5.0)
    try:
        reply = await module.handle_command(
            "face_id.enroll", {"track_id": "t1", "person_id": "p_sam", "name": "Sam"}
        )
    finally:
        await module.stop()
    assert reply["ok"] is True
    assert reply["person_id"] == "p_sam"
    assert list(store.persons) == ["p_sam"]
    assert store.persons["p_sam"].created_at == 1.0
    assert len(store.embeddings) == 6


async def test_enrolment_creates_the_person_even_without_a_name():
    store = StubStore()
    module = await track_one(store, enroll_timeout_s=5.0)
    try:
        reply = await module.handle_command(
            "face_id.enroll", {"track_id": "t1", "person_id": "p_new"}
        )
    finally:
        await module.stop()
    assert reply["ok"] is True
    assert store.persons["p_new"].name is None
