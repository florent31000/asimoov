"""`Go2Body`: connection lifecycle, gestures, gaze, percepts, e-stop."""

from __future__ import annotations

import asyncio
import dataclasses
import time

import pytest

from asimoov.bodies.go2 import sport as sport_mod
from asimoov.bodies.go2.adapter import (
    GAZE_DEAD_ZONE_DEG,
    GAZE_MAX_RX,
    Go2Body,
    gaze_yaw_rx,
    load_manifest,
)
from asimoov.contracts.behaviors import BehaviorStatus
from asimoov.contracts.body import BodyContext, GazeTarget
from asimoov.contracts.face import FaceState


def sport_requests(pub_sub) -> list[int]:
    return [
        options["api_id"]
        for topic, options in pub_sub.requests
        if topic == sport_mod.TOPIC_SPORT
    ]


async def started(body: Go2Body, ctx: BodyContext | None = None) -> Go2Body:
    await body.start(ctx or BodyContext())
    return body


def move_replies(bus) -> list[dict]:
    return [data for topic, data, _ in bus.published if topic == "body.reply"]


# -- lifecycle --------------------------------------------------------------


async def test_start_is_non_blocking_and_connects(body: Go2Body) -> None:
    began = time.monotonic()
    await body.start(BodyContext())
    assert time.monotonic() - began < 0.1

    health = await body.health()
    assert health.connected
    await body.stop()


async def test_start_never_raises_when_the_robot_is_absent(monkeypatch) -> None:
    monkeypatch.setattr("asimoov.bodies.go2.adapter.CONNECT_BACKOFF_S", (0.01, 0.02))
    attempts: list[float] = []

    async def connect(config):
        attempts.append(time.monotonic())
        raise OSError("no route to host")

    body = Go2Body({}, connect=connect)
    await body.start(BodyContext())
    await asyncio.sleep(0.1)

    health = await body.health()
    assert not health.connected
    assert any("no route to host" in error for error in health.errors)
    assert len(attempts) >= 3, "the connect loop must keep retrying"
    await body.stop()


async def test_connect_attempt_is_bounded_by_its_own_timeout(monkeypatch) -> None:
    monkeypatch.setattr("asimoov.bodies.go2.adapter.CONNECT_BACKOFF_S", (0.01,))

    async def connect(config):
        await asyncio.sleep(10)

    body = Go2Body({"connect_timeout_s": 0.05}, connect=connect)
    await body.start(BodyContext())
    await asyncio.sleep(0.15)

    health = await body.health()
    assert not health.connected
    assert any("timed out" in error for error in health.errors)
    await body.stop()


async def test_init_reports_a_motion_mode_the_robot_did_not_change(
    body: Go2Body, connection, monkeypatch
) -> None:
    monkeypatch.setattr(sport_mod, "MODE_SWITCH_SETTLE_S", 0.0)
    connection.pub_sub.mode_name = "ai"
    connection.pub_sub.mode_switch_applies = False
    await started(body)
    await asyncio.sleep(0.05)

    health = await body.health()
    assert any("motion mode" in error for error in health.errors)
    await body.stop()


async def test_init_switches_mode_and_enables_obstacle_avoidance(
    body: Go2Body, connection
) -> None:
    connection.pub_sub.mode_name = "normal"
    await started(body)
    topics = [topic for topic, _ in connection.pub_sub.requests]
    assert sport_mod.TOPIC_MOTION_SWITCHER in topics
    assert sport_mod.TOPIC_OBSTACLES_AVOID in topics
    await body.stop()


async def test_stop_leaves_no_task_behind(body: Go2Body) -> None:
    await started(body)
    await body.stop()
    await asyncio.sleep(0)
    names = {"go2-connect", "go2-motion-sender", "go2-telemetry", "go2-link-monitor"}
    assert not [t for t in asyncio.all_tasks() if t.get_name() in names]


# -- gestures ---------------------------------------------------------------


async def test_gesture_waits_for_the_real_reply(body: Go2Body, connection) -> None:
    await started(body)
    result = await body.gesture("wave_hello", {}, timeout_s=2.0)
    assert result.status is BehaviorStatus.OK
    assert 1016 in sport_requests(connection.pub_sub)
    await body.stop()


async def test_gesture_stands_up_first_then_skips_it_when_already_standing(
    body: Go2Body, connection
) -> None:
    await started(body)
    await body.gesture("wave_hello", {}, timeout_s=2.0)
    assert sport_requests(connection.pub_sub) == [1006, 1016]

    await body.gesture("greet", {}, timeout_s=2.0)
    assert sport_requests(connection.pub_sub) == [1006, 1016, 1016]
    await body.stop()


async def test_gesture_timeout_reports_timeout(body: Go2Body, connection) -> None:
    await started(body)
    connection.pub_sub.hang.add(sport_mod.TOPIC_SPORT)
    result = await body.gesture("stand_up", {}, timeout_s=0.1)
    assert result.status is BehaviorStatus.TIMEOUT
    assert connection.pub_sub.pending == {}
    await body.stop()


async def test_gesture_refused_by_the_robot_is_an_error(body: Go2Body, connection) -> None:
    await started(body)
    connection.pub_sub.status_code = 3203
    result = await body.gesture("stand_up", {}, timeout_s=1.0)
    assert result.status is BehaviorStatus.ERROR
    assert "3203" in (result.reason or "")
    await body.stop()


async def test_forbidden_gesture_never_reaches_the_robot(connection) -> None:
    manifest = load_manifest()
    manifest = dataclasses.replace(
        manifest,
        implements={**manifest.implements, "flip": {"primitive": "sport", "arg": "BackFlip"}},
    )

    async def connect(config):
        return connection

    body = Go2Body({}, manifest=manifest, connect=connect)
    await started(body)
    result = await body.gesture("flip", {}, timeout_s=1.0)

    assert result.status is BehaviorStatus.ERROR
    assert 1044 not in sport_requests(connection.pub_sub)
    await body.stop()


async def test_gaze_primitive_is_not_a_gesture(body: Go2Body) -> None:
    await started(body)
    result = await body.gesture("look_at", {}, timeout_s=1.0)
    assert result.status is BehaviorStatus.UNSUPPORTED
    await body.stop()


async def test_gesture_before_connection_is_an_error(body: Go2Body) -> None:
    result = await body.gesture("wave_hello", {}, timeout_s=1.0)
    assert result.status is BehaviorStatus.ERROR


# -- motion and gaze --------------------------------------------------------


async def test_move_is_long_and_starts_the_joystick(body: Go2Body, connection) -> None:
    await started(body)
    result = await body.move(1.0, 0.0, 0.0, 0.3)
    assert result.status is BehaviorStatus.STARTED
    assert result.eta_s == pytest.approx(0.3)

    await asyncio.sleep(0.25)
    assert connection.pub_sub.joystick, "no wireless-controller frame sent"
    assert all(frame[1] > 0 for frame in connection.pub_sub.joystick)
    await body.stop()


async def test_move_duration_is_capped_by_the_manifest(body: Go2Body) -> None:
    await started(body)
    result = await body.move(1.0, 0.0, 0.0, 600.0)
    assert result.eta_s == body.manifest.limits["max_continuous_motion_s"]
    await body.stop_all("test")
    await body.stop()


@pytest.mark.parametrize(
    ("az", "expected_sign"),
    [(30.0, -1.0), (-30.0, 1.0), (90.0, -1.0)],
)
def test_look_at_turns_toward_the_target(az: float, expected_sign: float) -> None:
    rx = gaze_yaw_rx(az)
    assert rx != 0.0
    assert rx / abs(rx) == expected_sign, "az > 0 is the robot's left, and left is rx < 0"
    assert abs(rx) <= GAZE_MAX_RX


@pytest.mark.parametrize("az", [0.0, 4.0, -7.9, GAZE_DEAD_ZONE_DEG])
def test_look_at_dead_zone(az: float) -> None:
    assert gaze_yaw_rx(az) == 0.0


async def test_look_at_drives_the_joystick_when_idle(body: Go2Body, connection) -> None:
    await started(body)
    await body.look_at(GazeTarget(az=40.0, el=0.0))
    await asyncio.sleep(0.25)
    assert connection.pub_sub.joystick
    assert all(frame[2] < 0 for frame in connection.pub_sub.joystick)
    await body.stop()


async def test_look_at_never_fights_a_commanded_motion(body: Go2Body, connection) -> None:
    await started(body)
    await body.move(1.0, 0.0, 0.0, 1.0)
    await body.look_at(GazeTarget(az=40.0, el=0.0))
    await asyncio.sleep(0.15)
    assert all(frame[2] == 0 for frame in connection.pub_sub.joystick)
    await body.stop()


async def test_stop_all_is_fast_and_idempotent(body: Go2Body, connection) -> None:
    await started(body)
    await body.move(1.0, 0.0, 0.0, 5.0)
    await asyncio.sleep(0.15)

    began = time.monotonic()
    await body.stop_all("test")
    elapsed = time.monotonic() - began
    assert elapsed < 0.2

    await body.stop_all("again")
    assert connection.pub_sub.joystick[-1] == (0.0, 0.0, 0.0, 0.0, 0)
    await asyncio.sleep(0.1)
    assert 1003 in sport_requests(connection.pub_sub), "StopMove must be sent"
    await body.stop()


async def test_move_reply_is_ok_when_the_move_ran_to_its_deadline(body: Go2Body, bus) -> None:
    await started(body, BodyContext(bus=bus))
    result = await body.move(1.0, 0.0, 0.0, 0.2)
    await asyncio.sleep(0.35)

    assert move_replies(bus) == [
        {"action_id": result.action_id, "name": "move", "status": "ok", "reason": None}
    ]
    await body.stop()


async def test_move_reply_is_interrupted_when_the_move_is_stopped(body: Go2Body, bus) -> None:
    await started(body, BodyContext(bus=bus))
    result = await body.move(1.0, 0.0, 0.0, 0.3)
    await asyncio.sleep(0.1)
    await body.stop_all("e-stop")
    await asyncio.sleep(0.35)

    reply = move_replies(bus)[0]
    assert reply["action_id"] == result.action_id
    assert reply["status"] == "interrupted"
    assert reply["reason"]
    await body.stop()


async def test_move_reply_is_an_error_when_the_link_refuses_the_frames(
    body: Go2Body, connection, bus
) -> None:
    await started(body, BodyContext(bus=bus))

    def refuse(topic, data=None, msg_type=None):  # noqa: ANN001, ANN202
        if any(data[axis] for axis in ("lx", "ly", "rx", "ry")):
            raise RuntimeError("data channel closed")

    connection.pub_sub.publish_without_callback = refuse
    await body.move(1.0, 0.0, 0.0, 0.3)
    await asyncio.sleep(0.45)

    reply = move_replies(bus)[0]
    assert reply["status"] == "error"
    assert "data channel closed" in reply["reason"]
    health = await body.health()
    assert any("data channel closed" in error for error in health.errors)
    await body.stop()


async def test_set_face_is_a_noop(body: Go2Body) -> None:
    await started(body)
    await body.set_face(FaceState(emotion="happy"))
    await body.stop()


# -- telemetry and disconnection -------------------------------------------


async def test_battery_and_body_state_percepts(body: Go2Body, connection, bus) -> None:
    await started(body, BodyContext(bus=bus))
    connection.pub_sub.emit(
        sport_mod.TOPIC_LOW_STATE, {"bms_state": {"soc": 76, "current": -2.0}}
    )
    connection.pub_sub.emit(sport_mod.TOPIC_SPORT_MODE_STATE, {"velocity": [0.4, 0.0, 0.0]})
    await asyncio.sleep(0.7)

    topics = {topic: data for topic, data, _ in bus.published}
    assert topics["percept.battery"]["level"] == pytest.approx(0.76)
    assert topics["percept.battery"]["charging"] is False
    assert topics["percept.body_state"]["moving"] is True

    health = await body.health()
    assert health.battery == pytest.approx(0.76)
    assert health.last_rtt_ms is not None
    await body.stop()


async def test_battery_percept_is_published_on_change_only(body: Go2Body, connection, bus) -> None:
    await started(body, BodyContext(bus=bus))
    connection.pub_sub.emit(sport_mod.TOPIC_LOW_STATE, {"bms_state": {"soc": 76}})
    await asyncio.sleep(1.2)

    battery_events = [1 for topic, _, _ in bus.published if topic == "percept.battery"]
    assert len(battery_events) == 1
    await body.stop()


async def test_disconnect_stops_everything_and_reconnects(body: Go2Body, connection) -> None:
    assert body.manifest.safety["stop_on_disconnect"] is True
    await started(body)
    await body.move(1.0, 0.0, 0.0, 5.0)
    await asyncio.sleep(0.15)

    await body.simulate_disconnect()
    assert connection.pub_sub.joystick[-1] == (0.0, 0.0, 0.0, 0.0, 0)

    await asyncio.sleep(0.1)
    health = await body.health()
    assert health.connected, "the adapter must reconnect on its own"
    await body.stop()
