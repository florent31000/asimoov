"""`asimoov enroll` against a running robot, over the real hub.

The CLI is a bus client like any other: it connects to the hub, reads the
retained `scene.state` to find who is visible, and sends a `cmd` that the
perception process answers.
"""

from __future__ import annotations

import asyncio
from argparse import Namespace
from dataclasses import replace

import pytest
from e2e import ROBOTS

from asimoov import __main__ as cli
from asimoov.contracts.envelope import Envelope
from asimoov.contracts.percepts import Bearing, PersonSeen
from asimoov.contracts.vocab import TOPICS
from asimoov.core.config import load_robot_config
from asimoov.core.runtime import Runtime

SEEN = PersonSeen(
    track_id="t7",
    confidence=0.9,
    bearing=Bearing(az=5.0),
    distance_class="near",
    bbox_norm=(0.4, 0.3, 0.6, 0.7),
    face_quality=0.8,
)


@pytest.fixture
async def robot(monkeypatch):
    """A running avatar robot with its hub on a free port."""
    config = load_robot_config(ROBOTS / "avatar")
    config = replace(config, hub=replace(config.hub, host="127.0.0.1", port=0))
    runtime = Runtime.build(config, body_name="fake", voice_name="none", face_names=(), tick_s=0.05)
    await runtime.start()
    monkeypatch.setattr(cli, "HUB_PORT", runtime.hub.bound_port)
    try:
        yield runtime
    finally:
        await runtime.stop()


async def _stand_in_for_perception(runtime: Runtime, answer: dict) -> list[Envelope]:
    """Answer `perception.face_id.enroll` the way the real process would."""
    received: list[Envelope] = []

    async def on_command(envelope: Envelope) -> None:
        if envelope.kind != "cmd":
            return
        received.append(envelope)
        await runtime.bus.publish(
            envelope.topic, answer, kind="reply", corr=envelope.id
        )

    runtime.bus.subscribe(cli.ENROLL_TOPIC, on_command)
    return received


async def _wait_for_scene(runtime: Runtime) -> None:
    await runtime.bus.publish(TOPICS.PERCEPT_PREFIX + "person_seen", SEEN.to_dict())
    for _ in range(100):
        scene = runtime.bus.latest(TOPICS.SCENE_STATE)
        if scene is not None and scene.data["people"]:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the mind never published a scene with the person in it")


async def test_enroll_finds_the_visible_person_and_reports_the_result(robot, capsys):
    received = await _stand_in_for_perception(robot, {"ok": True, "samples": 5})
    await _wait_for_scene(robot)

    code = await cli._enroll(Namespace(name="Sam", robot=None, hub=None, track=None, timeout=12.0))

    assert code == 0
    assert received[0].data == {
        "track_id": "t7",
        "person_id": "person:sam",
        "name": "Sam",
    }
    assert "enrolled Sam (5 samples)" in capsys.readouterr().out


async def test_enroll_reaches_a_robot_named_by_its_hub_url(robot, capsys, monkeypatch):
    """`--hub` talks to a robot running elsewhere, without a robot directory."""
    received = await _stand_in_for_perception(robot, {"ok": True, "samples": 3})
    await _wait_for_scene(robot)
    # Nothing is listening on the default port: only `--hub` can find it.
    monkeypatch.setattr(cli, "HUB_PORT", 1)

    code = await cli._enroll(
        Namespace(
            name="Sam",
            robot=None,
            hub=f"ws://127.0.0.1:{robot.hub.bound_port}",
            track=None,
            timeout=12.0,
        )
    )

    assert code == 0
    assert received[0].data["person_id"] == "person:sam"
    assert "enrolled Sam (3 samples)" in capsys.readouterr().out


async def test_a_refused_enrolment_is_reported_not_swallowed(robot, capsys):
    await _stand_in_for_perception(robot, {"ok": False, "reason": "face too small"})
    await _wait_for_scene(robot)

    code = await cli._enroll(Namespace(name="Sam", robot=None, hub=None, track=None, timeout=12.0))

    assert code == 1
    assert "face too small" in capsys.readouterr().err


async def test_enroll_refuses_to_guess_when_nobody_is_visible(robot, capsys):
    await _stand_in_for_perception(robot, {"ok": True, "samples": 5})

    code = await cli._enroll(Namespace(name="Sam", robot=None, hub=None, track=None, timeout=12.0))

    assert code == 1
    assert "nobody is visible" in capsys.readouterr().err
