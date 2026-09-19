"""`SportClient`: timeouts that clean up, forbidden commands, init sequences."""

from __future__ import annotations

import asyncio

import pytest

from asimoov.bodies.go2 import sport as sport_mod
from asimoov.bodies.go2.sport import (
    ForbiddenCommandError,
    SportClient,
    UnknownCommandError,
    reply_status,
)

FORBIDDEN = ("FrontFlip", "BackFlip", "LeftFlip", "RightFlip", "Handstand")


@pytest.fixture
def client(pub_sub):
    return SportClient(pub_sub, forbidden=FORBIDDEN)


async def test_request_returns_the_reply_and_measures_rtt(client, pub_sub) -> None:
    reply = await client.sport("Hello")
    assert reply_status(reply) == 0
    assert pub_sub.requests[0][0] == sport_mod.TOPIC_SPORT
    assert pub_sub.requests[0][1]["api_id"] == 1016
    assert client.last_rtt_ms is not None


async def test_request_ids_are_unique(client, pub_sub) -> None:
    await client.sport("Hello")
    await client.sport("Sit")
    ids = [options["id"] for _, options in pub_sub.requests]
    assert len(set(ids)) == len(ids)


async def test_timeout_raises_and_drops_the_pending_future(client, pub_sub) -> None:
    pub_sub.hang.add(sport_mod.TOPIC_SPORT)

    with pytest.raises(asyncio.TimeoutError):
        await client.sport("Hello", timeout_s=0.05)

    assert pub_sub.pending == {}, "a timed-out request must not leak a pending future"
    assert client.last_rtt_ms is None


@pytest.mark.parametrize("name", FORBIDDEN)
async def test_forbidden_commands_never_reach_the_robot(client, pub_sub, name: str) -> None:
    with pytest.raises(ForbiddenCommandError):
        await client.sport(name)
    assert pub_sub.requests == []


async def test_unknown_command(client, pub_sub) -> None:
    with pytest.raises(UnknownCommandError):
        await client.sport("Moonwalk")
    assert pub_sub.requests == []


def test_send_joystick_is_fire_and_forget(client, pub_sub) -> None:
    client.send_joystick(0.1, 0.2, -0.3, 0.0)
    assert pub_sub.joystick == [(0.1, 0.2, -0.3, 0.0, 0)]
    assert pub_sub.requests == []


async def test_ensure_normal_mode_skips_the_switch_when_already_normal(client, pub_sub) -> None:
    pub_sub.mode_name = "normal"
    assert await client.ensure_normal_mode()
    assert len(pub_sub.requests) == 1


async def test_ensure_normal_mode_switches_when_in_ai_mode(client, pub_sub) -> None:
    pub_sub.mode_name = "ai"
    assert await client.ensure_normal_mode(settle_s=0.0)
    topics = [topic for topic, _ in pub_sub.requests]
    assert topics == [sport_mod.TOPIC_MOTION_SWITCHER] * 3, "read, switch, read back"
    assert pub_sub.requests[1][1]["parameter"] == {"name": "normal"}


async def test_ensure_normal_mode_reports_a_switch_the_robot_ignored(client, pub_sub) -> None:
    pub_sub.mode_name = "ai"
    pub_sub.mode_switch_applies = False
    assert await client.ensure_normal_mode(settle_s=0.0) is False


async def test_obstacle_avoidance_tries_the_alternate_payloads(client, pub_sub) -> None:
    pub_sub.obstacle_ok_on_attempt = 3
    assert await client.set_obstacle_avoidance(True)
    assert len(pub_sub.requests) == 3
    assert pub_sub.requests[2][1]["parameter"] == {"data": True}


async def test_obstacle_avoidance_reports_failure(client, pub_sub) -> None:
    pub_sub.obstacle_ok_on_attempt = 99
    assert await client.set_obstacle_avoidance(True) is False
