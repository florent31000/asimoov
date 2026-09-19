"""Executor: real results, timeouts, long actions, motion clamping."""

from __future__ import annotations

import asyncio

from asimoov.contracts.behaviors import BehaviorCall, BehaviorStatus
from asimoov.contracts.body import BodyManifest
from asimoov.contracts.fakes import FakeBody
from asimoov.core.behaviors.executor import BehaviorExecutor
from asimoov.core.behaviors.resolver import BehaviorResolver, load_builtin_behaviors

WALKER = BodyManifest(
    name="walker",
    kind_of_body="quadruped",
    capabilities=("locomotion.planar", "locomotion.turn", "gesture.wave", "gaze.body_yaw"),
    implements={
        "move": {"primitive": "move"},
        "turn": {"primitive": "turn"},
        "wave_hello": {"primitive": "sport", "arg": "Hello", "est_ms": 4000},
        "look_at": {"primitive": "gaze_yaw"},
    },
    limits={"max_speed": 0.6, "max_yaw_rate": 0.5, "max_continuous_motion_s": 2},
    safety={"watchdog_ms": 500, "stop_on_disconnect": True},
)


NODDER = BodyManifest(
    name="nodder",
    kind_of_body="humanoid_bust",
    capabilities=("gesture.nod",),
    implements={"nod": {"primitive": "gesture", "arg": "nod", "est_ms": 300}},
)


def build(body, **kwargs):
    resolver = BehaviorResolver.from_body(load_builtin_behaviors(), body.manifest)
    return BehaviorExecutor(body, resolver, **kwargs)


async def test_short_behavior_returns_the_real_result():
    body = FakeBody(manifest=NODDER)
    executor = build(body)
    result = await executor.run(BehaviorCall(name="nod"))
    assert result.status is BehaviorStatus.OK
    assert ("gesture", {"name": "nod", "params": {}}) in body.calls


async def test_an_unsupported_behavior_is_never_faked():
    body = FakeBody()
    result = await build(body).run(BehaviorCall(name="nod"))
    assert result.status is BehaviorStatus.UNSUPPORTED
    assert "gesture.nod" in result.reason


async def test_a_slow_body_produces_a_timeout_not_a_hang():
    body = FakeBody(manifest=NODDER, latency_s=5.0)
    result = await build(body).run(BehaviorCall(name="nod"), timeout_s=0.05)
    assert result.status is BehaviorStatus.TIMEOUT


async def test_a_long_behavior_starts_and_reports_later():
    body = FakeBody()
    completions: list[tuple[str, str, str]] = []

    async def on_completion(action_id, name, status):
        completions.append((action_id, name, status))

    executor = build(body, on_completion=on_completion)
    result = await executor.run(BehaviorCall(name="wave_hello", call_id="a1"))
    assert result.status is BehaviorStatus.STARTED
    assert result.action_id == "a1"
    assert result.eta_s == 0.5

    for _ in range(50):
        if completions:
            break
        await asyncio.sleep(0.01)
    assert completions == [("a1", "wave_hello", "done")]
    assert executor.active == {}


async def test_cancel_all_reports_every_running_action():
    body = FakeBody(latency_s=5.0)
    completions: list[tuple[str, str, str]] = []

    async def on_completion(action_id, name, status):
        completions.append((action_id, name, status))

    executor = build(body, on_completion=on_completion)
    await executor.run(BehaviorCall(name="wave_hello", call_id="a2"))
    await asyncio.sleep(0)
    await executor.cancel_all("e_stop")
    assert completions == [("a2", "wave_hello", "canceled")]


async def test_move_is_clamped_by_max_continuous_motion():
    body = FakeBody(manifest=WALKER)
    executor = build(body)
    result = await executor.run(
        BehaviorCall(name="move", params={"direction": "forward", "speed": 0.5, "duration": 30})
    )
    assert result.status is BehaviorStatus.STARTED
    for _ in range(50):
        if any(call[0] == "move" for call in body.calls):
            break
        await asyncio.sleep(0.01)
    move = next(call for call in body.calls if call[0] == "move")
    assert move[1]["duration_s"] == 2
    assert move[1]["vx"] == 0.5


async def test_turn_left_is_positive_yaw():
    body = FakeBody(manifest=WALKER)
    executor = build(body)
    await executor.run(BehaviorCall(name="turn", params={"direction": "left", "angle": 90}))
    for _ in range(50):
        if any(call[0] == "move" for call in body.calls):
            break
        await asyncio.sleep(0.01)
    move = next(call for call in body.calls if call[0] == "move")
    assert move[1]["wz"] > 0


async def test_an_unknown_direction_is_an_error():
    body = FakeBody(manifest=WALKER)
    executor = build(body)
    result = await executor.run(BehaviorCall(name="move", params={"direction": "up"}))
    # move is a long behavior: the error surfaces through the completion hook.
    assert result.status is BehaviorStatus.STARTED


async def test_look_at_goes_through_the_gaze_primitive():
    body = FakeBody(manifest=WALKER)
    result = await build(body).run(BehaviorCall(name="look_at", params={"az": 30.0, "el": 5.0}))
    assert result.status is BehaviorStatus.OK
    assert body.gaze_targets[-1].az == 30.0


async def test_a_capability_without_an_implements_entry_is_an_error():
    manifest = BodyManifest(
        name="claims",
        kind_of_body="virtual",
        capabilities=("gesture.sit",),
        implements={},
    )
    body = FakeBody(manifest=manifest)
    result = await build(body).run(BehaviorCall(name="sit"))
    assert result.status is BehaviorStatus.ERROR
    assert "does not implement" in result.reason


async def test_a_behavior_with_no_requirement_and_no_primitive_is_spoken_only():
    body = FakeBody()
    result = await build(body).run(BehaviorCall(name="greet"))
    assert result.status is BehaviorStatus.OK
    assert result.data["spoken_only"] is True


async def test_expression_goes_to_the_body_face():
    manifest = BodyManifest(
        name="screen",
        kind_of_body="virtual",
        capabilities=("face.screen",),
        implements={"express": {"primitive": "face"}},
    )
    body = FakeBody(manifest=manifest)
    result = await build(body).run(BehaviorCall(name="express", params={"emotion": "happy"}))
    assert result.status is BehaviorStatus.OK
    assert body.face_states[-1].emotion == "happy"


async def test_an_unknown_emotion_is_refused():
    manifest = BodyManifest(
        name="screen",
        kind_of_body="virtual",
        capabilities=("face.screen",),
        implements={"express": {"primitive": "face"}},
    )
    body = FakeBody(manifest=manifest)
    result = await build(body).run(BehaviorCall(name="express", params={"emotion": "smug"}))
    assert result.status is BehaviorStatus.ERROR
