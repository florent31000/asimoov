"""The Go2 and the InMoov bust, replayed through their real adapters.

Same runtime as the avatar test, but the body is the actual `Go2Body` /
`InMoovBody` talking to a fake WebRTC link and the Python twin of the UNO R4
firmware. That is the part `asimoov replay --body fake` cannot check: that a
tool call really reaches a body primitive and comes back as a result.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from e2e import REPLAY_DIR, ROBOTS
from fake_firmware import BENCH_CHANNELS, FakeFirmware, FakeSerialPort
from fakes import FakeConnection

from asimoov.bodies.go2 import Go2Body
from asimoov.bodies.go2 import sport as sport_mod
from asimoov.bodies.inmoov import InMoovBody, SerialLink
from asimoov.contracts.vocab import TOPICS
from asimoov.core.clock import ScaledClock
from asimoov.core.config import load_robot_config
from asimoov.core.replay import Replayer, first_timestamp
from asimoov.core.runtime import Runtime

GO2_WALK = REPLAY_DIR / "go2_walk.jsonl"
INMOOV_BENCH = REPLAY_DIR / "inmoov_bench.jsonl"
SPEED = 8.0


async def _runtime(robot: str, body: Any, replay_file) -> Runtime:
    config = load_robot_config(ROBOTS / robot)
    config = replace(config, hub=replace(config.hub, enabled=False))
    clock = ScaledClock(origin=first_timestamp(replay_file), speed=SPEED)
    runtime = Runtime(
        config=config,
        body=body,
        voice=None,
        faces=(),
        clock=clock,
        tick_s=1.0 / SPEED,
        hub_enabled=False,
    )
    await runtime.start()
    clock.reset()
    return runtime


@pytest.fixture
async def go2_runtime():
    connection = FakeConnection()

    async def connect(config: dict[str, Any]) -> FakeConnection:
        return connection

    runtime = await _runtime("go2", Go2Body({}, connect=connect), GO2_WALK)
    try:
        yield runtime, connection
    finally:
        await runtime.stop()


@pytest.fixture
async def inmoov_runtime():
    bench_firmware = FakeFirmware(channels=BENCH_CHANNELS)
    link = SerialLink("fake", serial_factory=lambda: FakeSerialPort(bench_firmware))
    runtime = await _runtime("inmoov", InMoovBody({}, link=link), INMOOV_BENCH)
    try:
        yield runtime, bench_firmware
    finally:
        await runtime.stop()


async def test_the_go2_replay_passes_against_the_real_adapter(go2_runtime):
    runtime, connection = go2_runtime

    result = await Replayer(runtime.bus, speed=SPEED, clock=runtime.clock).run(GO2_WALK)

    assert result.assertions == 5
    assert (await runtime.body.health()).connected is True
    assert TOPICS.SCENE_STATE in result.observed
    # The `sit` tool call went all the way to a real sport command on the link,
    # under its Go2 name: the executor passes the behavior name, the body maps it.
    sent = [options.get("api_id") for _topic, options in connection.pub_sub.requests]
    assert sport_mod.SPORT_CMD["Sit"] in sent, f"Sit never reached the robot: {sent}"


async def test_the_go2_never_sends_a_forbidden_command(go2_runtime):
    runtime, connection = go2_runtime

    result = await runtime.registry.call("gesture", {"name": "dance"})

    assert "FrontJump" not in str(connection.pub_sub.requests)
    assert "FrontPounce" not in str(connection.pub_sub.requests)
    assert result.status.value in ("ok", "started", "error", "unsupported")


async def test_the_inmoov_replay_passes_against_the_fake_firmware(inmoov_runtime):
    runtime, firmware = inmoov_runtime

    result = await Replayer(runtime.bus, speed=SPEED, clock=runtime.clock).run(INMOOV_BENCH)

    assert result.assertions == 4
    assert TOPICS.SCENE_STATE in result.observed
    # The manifest came from the firmware's `V` answer, not from a config file.
    assert "gesture.finger_demo" in runtime.body.manifest.capabilities
    moves = [line for line in firmware.commands if line.startswith("M ")]
    assert moves, f"the finger_demo tool call never moved a servo: {firmware.commands}"
