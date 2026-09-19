"""Tool registry: dynamic enums, real results, builtins, timeouts."""

from __future__ import annotations

import asyncio

import pytest

from asimoov.contracts.behaviors import BehaviorStatus
from asimoov.contracts.body import BodyManifest
from asimoov.contracts.envelope import Envelope
from asimoov.contracts.fakes import FakeBody, InMemoryMemoryStore
from asimoov.contracts.memory import Fact, Person
from asimoov.contracts.percepts import Bearing
from asimoov.contracts.tools import ToolResult, ToolSpec
from asimoov.core.behaviors.executor import BehaviorExecutor
from asimoov.core.behaviors.resolver import BehaviorResolver, load_builtin_behaviors
from asimoov.core.memory.sqlite_store import SqliteMemoryStore
from asimoov.core.safety import SafetyGuard
from asimoov.core.scene.scene import Presence, SocialScene
from asimoov.core.tools.builtin import build_builtin_tools
from asimoov.core.tools.registry import ToolRegistry, build_behavior_tools

WALKER = BodyManifest(
    name="walker",
    kind_of_body="quadruped",
    capabilities=("locomotion.planar", "locomotion.turn", "gesture.wave", "face.screen"),
    implements={
        "move": {"primitive": "move"},
        "turn": {"primitive": "turn"},
        "wave_hello": {"primitive": "sport", "arg": "Hello", "est_ms": 1000},
        "look_at": {"primitive": "gaze_yaw"},
        "express": {"primitive": "face"},
    },
)

SCENE = SocialScene(
    people=(
        Presence(track_id="t1", bearing=Bearing(az=30.0, el=2.0), name="Sam", person_id="person:sam"),
        Presence(track_id="t2", bearing=Bearing(az=-20.0, el=0.0)),
    ),
    attention_track_id="t1",
    speaker_track_id="t1",
)


class FakePerceptionBus:
    """A bus whose only subscriber is a face_id module. ``None`` = no module."""

    def __init__(self, enroll: dict | None = None, reload_gallery: dict | None = None):
        self.replies = {
            "perception.face_id.enroll": enroll,
            "perception.face_id.reload_gallery": reload_gallery,
        }
        self.requests: list[tuple[str, dict]] = []

    async def request(self, topic, data, *, timeout_s):
        self.requests.append((topic, dict(data)))
        reply = self.replies.get(topic)
        if reply is None:
            raise TimeoutError(f"no reply to {topic!r} within {timeout_s}s")
        return Envelope(kind="reply", topic=topic, src="perception", data=reply)


def enrolling_bus() -> FakePerceptionBus:
    return FakePerceptionBus(enroll={"ok": True, "samples": 7}, reload_gallery={"ok": True})


def build(body):
    resolver = BehaviorResolver.from_body(load_builtin_behaviors(), body.manifest)
    executor = BehaviorExecutor(body, resolver)
    registry = ToolRegistry()
    registry.register_all(
        build_behavior_tools(
            resolver, executor, emotions=("neutral", "happy"), scene_provider=lambda: SCENE
        )
    )
    return registry, body


def test_the_gesture_enum_only_lists_what_the_body_can_do():
    registry, _ = build(FakeBody())
    spec = registry.get("gesture").spec
    names = spec.params["properties"]["name"]["enum"]
    assert "wave_hello" in names
    assert "shake_hand" in names  # resolves through its fallback
    assert "nod" not in names
    assert registry.get("move") is None


def test_a_walker_gets_move_and_turn():
    registry, _ = build(FakeBody(manifest=WALKER))
    assert registry.get("move") is not None
    assert registry.get("turn") is not None
    assert registry.get("set_expression").spec.params["properties"]["emotion"]["enum"] == [
        "neutral",
        "happy",
    ]


async def test_calling_a_gesture_runs_the_behavior():
    registry, body = build(FakeBody())
    result = await registry.call("gesture", {"name": "wave_hello"})
    assert result.status is BehaviorStatus.STARTED
    await asyncio.sleep(0.05)
    assert any(call[0] == "gesture" for call in body.calls)


async def test_an_unknown_gesture_is_refused():
    registry, _ = build(FakeBody())
    result = await registry.call("gesture", {"name": "backflip"})
    assert result.status is BehaviorStatus.ERROR


async def test_an_unknown_tool_is_refused():
    registry, _ = build(FakeBody())
    result = await registry.call("teleport", {})
    assert result.status is BehaviorStatus.ERROR
    assert "unknown tool" in result.reason


async def test_look_at_resolves_a_name_to_a_bearing():
    registry, body = build(FakeBody(manifest=WALKER))
    result = await registry.call("look_at", {"target": "Sam"})
    assert result.status is BehaviorStatus.OK
    assert body.gaze_targets[-1].az == 30.0
    assert body.gaze_targets[-1].track_id == "t1"


async def test_look_at_ahead_is_zero():
    registry, body = build(FakeBody(manifest=WALKER))
    await registry.call("look_at", {"target": "ahead"})
    assert body.gaze_targets[-1].az == 0.0


async def test_look_at_an_absent_person_is_an_error():
    registry, _ = build(FakeBody(manifest=WALKER))
    result = await registry.call("look_at", {"target": "Caroline"})
    assert result.status is BehaviorStatus.ERROR


async def test_a_slow_tool_times_out():
    registry = ToolRegistry()

    async def slow(params):
        await asyncio.sleep(5)
        return ToolResult(status=BehaviorStatus.OK)

    registry.register(ToolSpec(name="slow", description="", timeout_s=0.05), slow)
    result = await registry.call("slow", {})
    assert result.status is BehaviorStatus.TIMEOUT


async def test_a_raising_tool_becomes_an_error_result():
    registry = ToolRegistry()

    async def broken(params):
        raise RuntimeError("nope")

    registry.register(ToolSpec(name="broken", description=""), broken)
    result = await registry.call("broken", {})
    assert result.status is BehaviorStatus.ERROR
    assert "nope" in result.reason


def test_registering_twice_is_refused_unless_replaced():
    registry = ToolRegistry()

    async def handler(params):
        return ToolResult(status=BehaviorStatus.OK)

    spec = ToolSpec(name="x", description="")
    registry.register(spec, handler)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(spec, handler)
    registry.register(spec, handler, replace=True)
    registry.unregister("x")
    assert registry.specs() == ()


async def test_who_is_here_reports_text_only():
    registry = ToolRegistry()
    registry.register_all(build_builtin_tools(scene_provider=lambda: SCENE))
    result = await registry.call("who_is_here", {})
    people = result.content["people"]
    assert people[0]["name"] is None or people[0]["name"] == "Sam"
    assert {person["known"] for person in people} == {True, False}
    assert all(set(person) == {"name", "known", "speaking", "where", "distance"} for person in people)


async def test_remember_person_creates_a_stable_identity(tmp_path):
    store = SqliteMemoryStore(tmp_path / "m.db")
    await store.open()
    bound: list[tuple[str, str, str]] = []

    class MindStub:
        def bind_person(self, track_id, person_id, name):
            bound.append((track_id, person_id, name))
            return True

    bus = enrolling_bus()
    registry = ToolRegistry()
    registry.register_all(
        build_builtin_tools(
            scene_provider=lambda: SCENE,
            memory=store,
            mind=MindStub(),
            bus=bus,
            clock=lambda: 5.0,
        )
    )
    try:
        result = await registry.call("remember_person", {"name": "Sam"})
        assert result.content["person_id"] == "person:sam"
        assert bound == [("t1", "person:sam", "Sam")]
        assert (await store.get_person("person:sam")).name == "Sam"

        # Calling it again reuses the same person, it does not duplicate.
        again = await registry.call("remember_person", {"name": "sam"})
        assert again.content["person_id"] == "person:sam"

        assert (await registry.call("remember_person", {"name": ""})).status is BehaviorStatus.ERROR
    finally:
        await store.close()


async def test_remember_fact_then_recall(tmp_path):
    store = SqliteMemoryStore(tmp_path / "m.db")
    await store.open()
    registry = ToolRegistry()
    registry.register_all(
        build_builtin_tools(
            scene_provider=lambda: SCENE,
            memory=store,
            bus=enrolling_bus(),
            clock=lambda: 7.0,
        )
    )
    try:
        await registry.call("remember_person", {"name": "Sam"})
        stored = await registry.call(
            "remember_fact", {"person": "Sam", "fact": "Sam builds an InMoov hand"}
        )
        assert stored.status is BehaviorStatus.OK
        recalled = await registry.call("recall", {"query": "InMoov"})
        assert "Sam builds an InMoov hand" in recalled.content["snippets"]

        unknown = await registry.call("remember_fact", {"person": "Zoe", "fact": "x"})
        assert unknown.status is BehaviorStatus.ERROR
        assert "remember_person" in unknown.reason
    finally:
        await store.close()


async def test_remember_person_enrolls_the_visible_face():
    """The tool must ask perception to learn the face, not just write a row."""
    store = InMemoryMemoryStore()
    bus = enrolling_bus()
    registry = ToolRegistry()
    registry.register_all(
        build_builtin_tools(scene_provider=lambda: SCENE, memory=store, bus=bus)
    )
    result = await registry.call("remember_person", {"name": "Zoe"})
    assert result.status is BehaviorStatus.OK
    assert result.content["samples"] == 7
    topic, payload = bus.requests[0]
    assert topic == "perception.face_id.enroll"
    assert payload["track_id"] == "t1"
    assert payload["person_id"] == "person:zoe"


async def test_remember_person_without_perception_is_an_error_not_a_fake_ok():
    store = InMemoryMemoryStore()
    registry = ToolRegistry()
    registry.register_all(
        build_builtin_tools(
            scene_provider=lambda: SCENE, memory=store, bus=FakePerceptionBus()
        )
    )
    result = await registry.call("remember_person", {"name": "Zoe"})
    assert result.status is BehaviorStatus.ERROR
    assert result.reason == "no perception"
    assert await store.find_person_by_name("Zoe") is None


async def test_remember_person_refused_by_perception_is_reported():
    store = InMemoryMemoryStore()
    bus = FakePerceptionBus(enroll={"ok": False, "reason": "no face in frame"})
    registry = ToolRegistry()
    registry.register_all(
        build_builtin_tools(scene_provider=lambda: SCENE, memory=store, bus=bus)
    )
    result = await registry.call("remember_person", {"name": "Zoe"})
    assert result.status is BehaviorStatus.ERROR
    assert result.reason == "no face in frame"


async def test_remember_person_keeps_relationship_and_notes():
    store = InMemoryMemoryStore()
    await store.upsert_person(
        Person(
            id="person:sam",
            name="Sam",
            created_at=1.0,
            relationship="son",
            notes="allergic to peanuts",
        )
    )
    registry = ToolRegistry()
    registry.register_all(
        build_builtin_tools(
            scene_provider=lambda: SCENE, memory=store, bus=enrolling_bus(), clock=lambda: 9.0
        )
    )
    assert (await registry.call("remember_person", {"name": "Sam"})).status is BehaviorStatus.OK
    stored = await store.get_person("person:sam")
    assert stored.relationship == "son"
    assert stored.notes == "allergic to peanuts"


async def test_forget_person_deletes_and_reloads_the_gallery(tmp_path):
    """'Forget me' must also invalidate the recognizer's in-RAM gallery."""
    store = SqliteMemoryStore(tmp_path / "m.db")
    await store.open()
    bus = enrolling_bus()
    registry = ToolRegistry()
    registry.register_all(
        build_builtin_tools(scene_provider=lambda: SCENE, memory=store, bus=bus)
    )
    try:
        await store.upsert_person(Person(id="person:sam", name="Sam", created_at=1.0))
        await store.add_fact(Fact(person_id="person:sam", text="likes robots", ts=2.0))
        result = await registry.call("forget_person", {"person": "Sam"})
        assert result.status is BehaviorStatus.OK
        assert result.content["gallery_reloaded"] is True
        assert await store.get_person("person:sam") is None
        assert await store.facts_for("person:sam", 5) == []
        assert bus.requests[-1][0] == "perception.face_id.reload_gallery"
    finally:
        await store.close()


async def test_forget_person_refuses_someone_it_does_not_know():
    registry = ToolRegistry()
    registry.register_all(
        build_builtin_tools(
            scene_provider=lambda: SCENE, memory=InMemoryMemoryStore(), bus=enrolling_bus()
        )
    )
    result = await registry.call("forget_person", {"person": "Zoe"})
    assert result.status is BehaviorStatus.ERROR


async def test_remember_fact_current_uses_the_speaker():
    store = InMemoryMemoryStore()
    registry = ToolRegistry()
    registry.register_all(build_builtin_tools(scene_provider=lambda: SCENE, memory=store))
    result = await registry.call("remember_fact", {"person": "current", "fact": "likes robots"})
    assert result.content["person_id"] == "person:sam"
    assert await store.recall("robots") == ["likes robots"]


async def test_the_stop_tool_goes_straight_to_the_safety_guard():
    body = FakeBody()
    guard = SafetyGuard(body)
    registry = ToolRegistry()
    registry.register_all(build_builtin_tools(scene_provider=lambda: SCENE, safety=guard))
    result = await registry.call("stop", {})
    assert result.status is BehaviorStatus.OK
    assert body.last_stop_all_reason == "tool"


def test_memory_tools_are_absent_without_a_store():
    tools = build_builtin_tools(scene_provider=lambda: SCENE)
    assert [spec.name for spec, _ in tools] == ["who_is_here"]
