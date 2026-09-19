"""The acceptance demo, as a test: hub up, face page served, replay green.

Mirrors ``asimoov run robots/avatar --voice fake --body avatar --face web
--replay tests/fixtures/replays/family_evening.jsonl``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from e2e import REPLAY_DIR, ROBOTS
from websockets.asyncio.client import connect

from asimoov.contracts.vocab import TOPICS
from asimoov.core.clock import ScaledClock
from asimoov.core.config import load_robot_config
from asimoov.core.replay import Replayer, first_timestamp
from asimoov.core.runtime import Runtime

FAMILY_EVENING = REPLAY_DIR / "family_evening.jsonl"
SPEED = 8.0


@pytest.fixture
async def avatar_runtime():
    """A whole avatar robot on a free port: hub, web face, fake voice."""
    config = load_robot_config(ROBOTS / "avatar")
    config = replace(config, hub=replace(config.hub, host="127.0.0.1", port=0))
    clock = ScaledClock(origin=first_timestamp(FAMILY_EVENING), speed=SPEED)
    runtime = Runtime.build(
        config,
        body_name="avatar",
        voice_name="fake",
        face_names=("web",),
        clock=clock,
        tick_s=1.0 / SPEED,
    )
    await runtime.start()
    clock.reset()
    try:
        yield runtime
    finally:
        await runtime.stop()


async def _http_get(port: int, path: str) -> tuple[int, bytes]:
    """One plain HTTP request on the hub's port, without adding a dependency."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(-1), 5.0)
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), body


async def test_the_hub_serves_the_face_page_and_its_assets(avatar_runtime):
    port = avatar_runtime.hub.bound_port

    status, body = await _http_get(port, "/face/")
    assert status == 200
    assert b"<canvas" in body

    for asset, marker in (("face.js", b"function"), ("face.css", b"{"), ("emotions.json", b"{")):
        status, body = await _http_get(port, f"/face/{asset}")
        assert status == 200, asset
        assert marker in body, asset

    assert (await _http_get(port, "/face"))[0] == 301
    assert (await _http_get(port, "/face/nope"))[0] == 404


async def test_a_face_page_connects_on_face_ws_and_receives_the_face(avatar_runtime):
    """WS1 routes `/face/ws` to the renderer the hub mounted automatically."""
    hub = avatar_runtime.hub
    url = f"ws://127.0.0.1:{hub.bound_port}/face/ws?token={hub.token}"
    async with connect(url) as page:
        await avatar_runtime.bus.publish(
            TOPICS.FACE_STATE,
            avatar_runtime.mind.face_state().to_dict(),
            kind="state",
        )
        envelope = json.loads(await asyncio.wait_for(page.recv(), 5.0))
    assert envelope["topic"] == TOPICS.FACE_STATE
    assert envelope["data"]["emotion"] in ("neutral", "happy", "curious")


async def test_the_family_evening_replay_passes_end_to_end(avatar_runtime):
    result = await Replayer(
        avatar_runtime.bus, speed=SPEED, clock=avatar_runtime.clock
    ).run(FAMILY_EVENING)

    assert result.assertions == 5
    assert result.envelopes == 13
    assert avatar_runtime.bus.latest(TOPICS.FACE_STATE) is not None
    # The tool results actually reached the provider, not just the bus.
    assert [call_id for call_id, _ in avatar_runtime.voice.tool_results] == [
        "call_sam_1",
        "call_sam_2",
    ]
