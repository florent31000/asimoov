"""Keyframes to ``M`` commands, and the double clamp."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fake_firmware import BUST_CHANNELS, FakeFirmware, make_tcp_body

from asimoov.bodies.inmoov.adapter import GESTURES_DIR
from asimoov.bodies.inmoov.gestures import (
    Gesture,
    GestureError,
    GestureRunner,
    Keyframe,
    load_gesture,
    load_gestures,
    to_commands,
)
from asimoov.bodies.inmoov.link import ChannelSpec
from asimoov.contracts.body import BodyContext
from asimoov.contracts.vocab import TOPICS, is_capability

BUST_SPECS = {
    channel.name: ChannelSpec(
        channel.id, channel.name, channel.min_deg, channel.max_deg, channel.rest_deg
    )
    for channel in BUST_CHANNELS
}


def test_shipped_gestures_are_valid() -> None:
    gestures = load_gestures(GESTURES_DIR)
    assert set(gestures) == {
        "shake_hand",
        "wave_hello",
        "nod",
        "shake_head",
        "point",
        "finger_demo",
    }
    for gesture in gestures.values():
        assert gesture.requires
        for capability in gesture.requires:
            assert is_capability(capability), capability


def test_keyframes_become_relative_moves() -> None:
    gesture = load_gesture(GESTURES_DIR / "shake_hand.yaml")
    commands = to_commands(gesture, BUST_SPECS)
    assert commands[0] == ("M 3:40,2:70,1:90,0:10 T0", 0)
    assert commands[1] == ("M 3:60,2:40,0:80 T900", 900)
    assert commands[2] == ("M 2:55 T500", 500)
    assert commands[-1][0].endswith("T900")
    assert gesture.duration_ms == 2800


def test_python_side_clamp_matches_the_firmware() -> None:
    gesture = Gesture(
        name="overshoot",
        requires=(),
        keyframes=(Keyframe(t_ms=100, pose={"neck_yaw": 999, "neck_pitch": -50}),),
    )
    assert to_commands(gesture, BUST_SPECS) == [("M 8:150,9:60 T100", 100)]


def test_unknown_joint_is_refused() -> None:
    gesture = Gesture("nope", (), (Keyframe(10, {"tail": 10}),))
    with pytest.raises(GestureError):
        to_commands(gesture, BUST_SPECS)


def test_malformed_files_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("name: bad\nkeyframes: []\n", encoding="utf-8")
    with pytest.raises(GestureError):
        load_gesture(path)

    path.write_text(
        "name: bad\nkeyframes:\n  - {t: 100, pose: {jaw: 10}}\n  - {t: 50, pose: {jaw: 20}}\n",
        encoding="utf-8",
    )
    with pytest.raises(GestureError):
        load_gesture(path)


async def test_runner_attaches_then_plays(tcp_link) -> None:
    link, firmware = tcp_link
    specs = {"finger_demo": ChannelSpec(100, "finger_demo", 2, 108, 2)}
    gesture = Gesture(
        "quick", ("gesture.finger_demo",), (Keyframe(10, {"finger_demo": 90}),)
    )
    await GestureRunner(link, specs).run(gesture)
    assert firmware.commands == ["E 100", "M 100:90 T10"]
    assert firmware.attached(100)


async def test_short_gesture_runs_inline_long_one_reports_started(
    bench_firmware: FakeFirmware,
) -> None:
    body, server = await make_tcp_body(bench_firmware)
    try:
        await body.start(BodyContext())
        result = await body.gesture("finger_demo", {}, timeout_s=3.0)
        assert result.status.value == "ok"
        assert "M 100:90 T600" in bench_firmware.commands

        unsupported = await body.gesture("shake_hand", {}, timeout_s=1.0)
        assert unsupported.status.value == "unsupported"
    finally:
        await body.stop()
        server.close()
        await server.wait_closed()


async def test_long_gesture_is_interrupted_by_an_estop() -> None:
    firmware = FakeFirmware(channels=BUST_CHANNELS)
    body, server = await make_tcp_body(firmware)
    try:
        await body.start(BodyContext())
        started = await body.gesture("shake_hand", {}, timeout_s=1.0)
        assert started.status.value == "started"
        assert started.eta_s == 2.8
        await asyncio.sleep(0.05)
        await body.stop_all("test")
        assert firmware.estopped
        sent = len(firmware.commands)
        await asyncio.sleep(0.2)
        assert len(firmware.commands) == sent
    finally:
        await body.stop()
        server.close()
        await server.wait_closed()


async def test_interrupting_a_gesture_freezes_instead_of_detaching() -> None:
    firmware = FakeFirmware(channels=BUST_CHANNELS)
    body, server = await make_tcp_body(firmware)
    try:
        await body.start(BodyContext())
        assert (await body.gesture("shake_hand", {}, timeout_s=1.0)).status.value == "started"
        await asyncio.sleep(0.05)

        assert (await body.gesture("wave_hello", {}, timeout_s=1.0)).status.value == "started"
        await asyncio.sleep(0.05)

        assert "!" not in firmware.commands
        assert not firmware.estopped
        freezes = [c for c in firmware.commands if c.startswith("M ") and c.endswith(" T0")]
        assert freezes, f"no freeze move was sent: {firmware.commands}"
        assert firmware.attached(3), "the shoulder was detached by the interruption"
    finally:
        await body.stop()
        server.close()
        await server.wait_closed()


async def test_a_cancelled_long_gesture_publishes_a_canceled_reply() -> None:
    firmware = FakeFirmware(channels=BUST_CHANNELS)
    body, server = await make_tcp_body(firmware)
    published: list[tuple[str, dict]] = []

    def publish(topic: str, data: dict, kind: str = "event") -> None:
        published.append((topic, data))

    try:
        await body.start(BodyContext(publish=publish))
        started = await body.gesture("shake_hand", {}, timeout_s=1.0)
        assert started.status.value == "started"
        await asyncio.sleep(0.05)
        await body.stop_all("test")
        await asyncio.sleep(0.05)

        replies = [data for topic, data in published if topic == TOPICS.BODY_REPLY]
        assert replies, f"no body.reply was published: {published}"
        assert replies[-1] == {
            "action_id": started.action_id,
            "name": "shake_hand",
            "status": "canceled",
            "reason": "interrupted",
        }
    finally:
        await body.stop()
        server.close()
        await server.wait_closed()
