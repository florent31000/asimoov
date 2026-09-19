"""Gaze sign convention, P-controller, jaw throttling and the LED face."""

from __future__ import annotations

import asyncio
import logging

from fake_firmware import BUST_CHANNELS, FakeFirmware, make_tcp_body

from asimoov.bodies.inmoov.faults import FaultTracker
from asimoov.bodies.inmoov.gaze_loop import DEAD_ZONE_DEG, MAX_RATE_DEG_S, RATE_HZ, GazeLoop
from asimoov.bodies.inmoov.jaw import JawDriver
from asimoov.bodies.inmoov.link import ChannelSpec
from asimoov.bodies.inmoov.servo_face import ServoFace
from asimoov.contracts.body import BodyContext, GazeTarget
from asimoov.contracts.face import FaceState

SPECS = {
    channel.name: ChannelSpec(
        channel.id, channel.name, channel.min_deg, channel.max_deg, channel.rest_deg
    )
    for channel in BUST_CHANNELS
}


class Clock:
    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def test_positive_azimuth_turns_the_neck_left(tcp_link) -> None:
    link, _ = tcp_link
    clock = Clock()
    loop = GazeLoop(link, SPECS, clock=clock)
    assert loop.available

    loop.set_target(GazeTarget(az=30.0, el=0.0))
    command = await loop.step()
    # neck_yaw rests at 90 and grows toward the robot's own left.
    assert command is not None
    yaw = int(command.split()[1].split(",")[0].split(":")[1])
    assert yaw > 90

    loop.set_target(GazeTarget(az=-30.0, el=0.0))
    for _ in range(6):
        clock.advance(1 / RATE_HZ)
        await loop.step()
    assert loop._yaw_cmd < 90


async def test_gaze_respects_dead_zone_and_max_rate(tcp_link) -> None:
    link, _ = tcp_link
    clock = Clock()
    loop = GazeLoop(link, SPECS, clock=clock)

    loop.set_target(GazeTarget(az=2.0, el=0.0))
    assert await loop.step() is None  # 2 deg is inside the 3 deg dead zone
    assert DEAD_ZONE_DEG == 3.0

    loop.set_target(GazeTarget(az=60.0, el=0.0))
    await loop.step()
    step_deg = loop._yaw_cmd - 90
    assert step_deg <= MAX_RATE_DEG_S / RATE_HZ + 1e-6


async def test_gaze_scans_slowly_when_idle(tcp_link) -> None:
    link, _ = tcp_link
    clock = Clock()
    loop = GazeLoop(link, SPECS, clock=clock)

    positions = []
    for _ in range(40):
        clock.advance(1 / RATE_HZ)
        await loop.step()
        positions.append(loop._yaw_cmd)
    assert max(positions) > 90
    assert max(positions) - min(positions) < 60


async def test_gaze_is_a_noop_without_a_neck(tcp_link) -> None:
    link, _ = tcp_link
    loop = GazeLoop(link, {"finger_demo": ChannelSpec(100, "finger_demo", 2, 108, 2)})
    assert not loop.available
    loop.set_target(GazeTarget(az=40.0, el=0.0))
    assert await loop.step() is None


async def test_jaw_throttles_on_threshold_and_rate(bust_tcp_link) -> None:
    link, firmware = bust_tcp_link
    clock = Clock()
    jaw = JawDriver(link, clock=clock)

    assert await jaw.update(0.0) is True
    assert await jaw.update(0.02) is False  # below the 5 % threshold
    clock.advance(0.5)
    assert await jaw.update(0.6) is True
    assert await jaw.update(0.9) is False  # 25 Hz ceiling
    clock.advance(0.04)
    assert await jaw.update(0.9) is True
    assert [c for c in firmware.commands if c.startswith("J ")] == ["J 0", "J 60", "J 90"]


async def test_servo_face_sends_emotion_once_and_moves_the_eyelids() -> None:
    firmware = FakeFirmware(channels=BUST_CHANNELS)
    body, server = await make_tcp_body(firmware)
    try:
        await body.start(BodyContext())
        await body.set_face(FaceState(emotion="curious", eyelids=0.0, lip=0.0))
        await body.set_face(FaceState(emotion="curious", eyelids=0.0, lip=0.0))
        assert firmware.commands.count("F curious") == 1
        assert firmware.emotion == "curious"

        await body.set_face(FaceState(emotion="curious", eyelids=1.0, lip=0.0))
        assert "S 11 90" in firmware.commands
    finally:
        await body.stop()
        server.close()
        await server.wait_closed()


async def test_servo_face_without_eyelids_only_drives_the_matrix(tcp_link) -> None:
    link, firmware = tcp_link
    face = ServoFace(link, {"finger_demo": ChannelSpec(100, "finger_demo", 2, 108, 2)})
    await face.start({})
    await face.render(FaceState(emotion="happy", eyelids=0.8))
    assert firmware.commands == ["F happy"]


async def test_the_head_channels_are_attached_on_connect() -> None:
    firmware = FakeFirmware(channels=BUST_CHANNELS)
    body, server = await make_tcp_body(firmware)
    try:
        await body.start(BodyContext())
        for name in ("neck_yaw", "neck_pitch", "jaw", "eyelids"):
            assert firmware.attached(SPECS[name].id), f"{name} is still limp after connect"
        assert not firmware.attached(SPECS["fingers_r"].id), "a gesture joint must stay detached"
    finally:
        await body.stop()
        server.close()
        await server.wait_closed()


async def test_repeated_face_failures_surface_in_health() -> None:
    firmware = FakeFirmware(channels=BUST_CHANNELS)
    body, server = await make_tcp_body(firmware)
    original = firmware.handle_line
    firmware.handle_line = lambda line: (
        ["ERR bad args"] if line.startswith("F ") else original(line)
    )
    try:
        await body.start(BodyContext())
        for _ in range(4):
            await body.set_face(FaceState(emotion="happy", eyelids=0.0, lip=0.0))
        errors = (await body.health()).errors
        assert any("emotion" in error for error in errors), errors

        firmware.handle_line = original
        await body.set_face(FaceState(emotion="happy", eyelids=0.0, lip=0.0))
        assert not any("emotion" in error for error in (await body.health()).errors)
    finally:
        await body.stop()
        server.close()
        await server.wait_closed()


async def test_a_failing_gaze_tick_is_logged_and_counted(tcp_link, caplog) -> None:
    link, _ = tcp_link
    faults = FaultTracker(threshold=1)
    loop = GazeLoop(link, SPECS, faults=faults)
    loop.set_target(GazeTarget(az=40.0, el=0.0))

    async def boom(line: str, *, timeout_s: float = 0.5) -> None:
        raise RuntimeError("cable chewed")

    link.send = boom
    with caplog.at_level(logging.ERROR):
        await loop.start()
        await asyncio.sleep(0.5)
        await loop.stop()

    assert faults.errors
    assert any(record.exc_info for record in caplog.records)
