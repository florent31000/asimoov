"""Interop with WS1's real hub: handshake, percepts, commands, binary frames.

Skipped if `asimoov.core.bus` is not present. These are the tests that catch a
protocol drift between the perception process and the core.
"""

from __future__ import annotations

import asyncio

import pytest
from helpers import synthetic_image

from asimoov.contracts.envelope import Envelope
from asimoov.contracts.frames import encode_frame
from asimoov.core.bus.client import BusClient

pytest.importorskip("asimoov.core.bus.hub", reason="WS1 core not present")

from asimoov.core.bus.hub import Hub  # noqa: E402
from asimoov.core.bus.local import LocalBus  # noqa: E402

TOKEN = "test-token"


class RunningHub:
    def __init__(self) -> None:
        self.bus = LocalBus(src="core")
        self.hub = Hub(self.bus, host="127.0.0.1", port=0, token=TOKEN)

    async def __aenter__(self) -> RunningHub:
        await self.hub.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.hub.stop()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.hub.bound_port}"


async def connected(url: str, **kwargs) -> BusClient:
    client = BusClient(url, TOKEN, **kwargs)
    await client.connect(5.0)
    return client


async def test_the_handshake_is_accepted_by_the_real_hub():
    async with RunningHub() as running:
        client = await connected(running.url, module_id="perception")
        assert client.connected is True
        await client.close()


async def test_a_bad_token_is_refused():
    async with RunningHub() as running:
        client = BusClient(running.url, "wrong", reconnect_min_s=5.0)
        with pytest.raises(asyncio.TimeoutError):
            await client.connect(0.5)
        await client.close()


async def test_a_percept_reaches_the_core_bus():
    seen: list[Envelope] = []

    async def handler(envelope: Envelope) -> None:
        seen.append(envelope)

    async with RunningHub() as running:
        running.bus.subscribe("percept.*", handler)
        client = await connected(running.url, module_id="perception.face_id")
        await client.publish("percept.person_seen", {"type": "person_seen", "track_id": "t1"})
        async with asyncio.timeout(5.0):
            while not seen:
                await asyncio.sleep(0.01)
        await client.close()
    assert seen[0].src == "perception.face_id"
    assert seen[0].data["track_id"] == "t1"


async def test_a_core_command_is_delivered_and_replied_to():
    received: list[Envelope] = []

    async def handler(envelope: Envelope) -> None:
        received.append(envelope)
        await client.reply(envelope, {"ok": True, "samples": 5})

    async with RunningHub() as running:
        client = await connected(running.url, module_id="perception")
        client.subscribe("perception.*", handler)
        await asyncio.sleep(0.2)  # let the pattern update reach the hub
        reply = await running.bus.request(
            "perception.face_id.enroll",
            {"track_id": "t1", "person_id": "p_sam"},
            timeout_s=5.0,
        )
        await client.close()
    assert received[0].data["person_id"] == "p_sam"
    assert reply.data == {"ok": True, "samples": 5}


async def test_a_binary_frame_reaches_the_ws_frames_camera():
    cv2 = pytest.importorskip("cv2", reason="needs the [vision] extra")
    from asimoov.perception.camera import WsFrameCamera

    ok, buffer = cv2.imencode(".jpg", synthetic_image(320, 180, seed=2))
    assert ok

    async with RunningHub() as running:
        producer = await connected(running.url, module_id="faces.web")
        consumer = await connected(running.url, module_id="perception")
        camera = WsFrameCamera(consumer, fps=1000)
        await camera.start()
        await asyncio.sleep(0.2)  # the hub only relays frames to frame.* subscribers
        for _ in range(5):
            await producer.send_frame(encode_frame("frame.browser", buffer.tobytes()))
            await asyncio.sleep(0.05)
        frame = await asyncio.wait_for(camera.read(), 5.0)
        await camera.stop()
        await consumer.close()
        await producer.close()
    assert (frame.width, frame.height) == (320, 180)
