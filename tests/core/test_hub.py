"""Hub: token gate, hello handshake, retained state, frames, HTTP hook."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from asimoov.contracts.envelope import Envelope
from asimoov.contracts.face import ProcessRequestResponse
from asimoov.contracts.vocab import TOPICS
from asimoov.core.bus.client import BusClient
from asimoov.core.bus.hub import SEND_QUEUE_MAX, Hub, _Client, read_or_create_token
from asimoov.core.bus.local import LocalBus

TOKEN = "test-token"


@pytest.fixture
async def hub():
    bus = LocalBus(src="core")
    hub = Hub(bus, host="127.0.0.1", port=0, token=TOKEN)
    await hub.start()
    try:
        yield hub
    finally:
        await hub.stop()


def test_token_is_created_once(asimoov_home):
    first = read_or_create_token()
    assert (asimoov_home / "token").is_file()
    assert read_or_create_token() == first


async def test_bus_client_round_trip(hub):
    received: list[Envelope] = []

    async def on_envelope(envelope: Envelope) -> None:
        received.append(envelope)

    client = BusClient(hub.url(), TOKEN, "tests.client", subscriptions=("percept.*",))
    client.subscribe("percept.*", on_envelope)
    await client.connect()
    try:
        from_client: list[Envelope] = []

        async def on_core(envelope: Envelope) -> None:
            from_client.append(envelope)

        hub.bus.subscribe("percept.*", on_core)

        await hub.bus.publish("percept.person_seen", {"track_id": "t1"})
        for _ in range(100):
            if received:
                break
            await asyncio.sleep(0.01)
        assert [envelope.data["track_id"] for envelope in received] == ["t1"]

        await client.publish("percept.touched", {"type": "touched", "where": "screen"})
        for _ in range(100):
            if any(envelope.src == "tests.client" for envelope in from_client):
                break
            await asyncio.sleep(0.01)
        assert any(envelope.data.get("where") == "screen" for envelope in from_client)
    finally:
        await client.close()


async def test_retained_state_is_sent_on_connect(hub):
    await hub.bus.publish(TOPICS.SCENE_STATE, {"people": [], "attention_track_id": None}, kind="state")
    received: list[Envelope] = []

    async def on_envelope(envelope: Envelope) -> None:
        received.append(envelope)

    client = BusClient(hub.url(), TOKEN, "tests.late", subscriptions=(TOPICS.SCENE_STATE,))
    client.subscribe(TOPICS.SCENE_STATE, on_envelope)
    await client.connect()
    try:
        for _ in range(100):
            if received:
                break
            await asyncio.sleep(0.01)
        assert received and received[0].topic == TOPICS.SCENE_STATE
    finally:
        await client.close()


async def test_a_wrong_token_is_rejected(hub):
    with pytest.raises(InvalidStatus) as error:
        async with connect(f"{hub.url()}?token=wrong"):
            pass
    assert error.value.response.status_code == 401


async def test_binary_frames_never_reach_the_core_bus(hub):
    core_seen: list[Envelope] = []

    async def on_core(envelope: Envelope) -> None:
        core_seen.append(envelope)

    hub.bus.subscribe("*", on_core)

    viewer_frames: list[bytes] = []
    async with connect(f"{hub.url()}?token={TOKEN}") as viewer:
        await viewer.send(json.dumps({"type": "hello", "module_id": "viewer", "subscribe": ["frame.*"]}))
        assert json.loads(await viewer.recv())["type"] == "welcome"

        async with connect(f"{hub.url()}?token={TOKEN}") as camera:
            await camera.send(json.dumps({"type": "hello", "module_id": "camera", "subscribe": []}))
            assert json.loads(await camera.recv())["type"] == "welcome"
            await camera.send(b"\x00" * 8 + b"jpegbytes")
            viewer_frames.append(await asyncio.wait_for(viewer.recv(), timeout=2.0))

    assert viewer_frames == [b"\x00" * 8 + b"jpegbytes"]
    assert core_seen == []


async def test_the_first_message_must_be_a_hello(hub):
    async with connect(f"{hub.url()}?token={TOKEN}") as connection:
        await connection.send(json.dumps({"type": "not-hello"}))
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(connection.recv(), timeout=2.0)


async def test_the_face_hook_serves_plain_http(hub):
    def hook(path: str):
        if path == "/face":
            return ProcessRequestResponse(
                status=200, headers=(("Content-Type", "text/html"),), body=b"<html>eyes</html>"
            )
        return None

    hub.set_process_request_hook(hook)

    def fetch(path: str) -> tuple[int, bytes]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{hub.bound_port}{path}", timeout=2) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read()

    assert await asyncio.to_thread(fetch, "/face") == (200, b"<html>eyes</html>")
    status, _ = await asyncio.to_thread(fetch, "/nope")
    assert status == 404


async def test_a_routed_websocket_path_bypasses_the_bus_handshake(hub):
    seen: list[str] = []

    async def attach(connection, *, path=None):
        seen.append(path or "")
        await connection.send("attached")
        await connection.close()

    hub.route("/face/ws", attach)
    async with connect(f"ws://127.0.0.1:{hub.bound_port}/face/ws?token=page") as connection:
        assert await connection.recv() == "attached"
    assert seen == ["/face/ws?token=page"]


async def test_an_unknown_websocket_path_is_not_upgraded(hub):
    with pytest.raises(InvalidStatus) as error:
        async with connect(f"ws://127.0.0.1:{hub.bound_port}/ghost"):
            pass
    assert error.value.response.status_code == 404


async def test_a_reply_reaches_the_client_that_sent_the_command(hub):
    """The hub must not mistake a reply for an echo of the command."""

    async def responder(envelope: Envelope) -> None:
        if envelope.kind == "cmd":
            await hub.bus.publish(
                envelope.topic, {"ok": True, "samples": 5}, kind="reply", corr=envelope.id
            )

    hub.bus.subscribe("perception.*", responder)

    client = BusClient(hub.url(), TOKEN, "tests.cli")
    await client.connect()
    try:
        reply = await client.request(
            "perception.face_id.enroll", {"track_id": "t1"}, timeout_s=3.0
        )
    finally:
        await client.close()
    assert reply.data == {"ok": True, "samples": 5}


async def test_a_client_is_still_not_echoed_its_own_envelope(hub):
    """It sees its own publish once, locally -- not a second time off the hub."""
    seen: list[Envelope] = []

    async def on_envelope(envelope: Envelope) -> None:
        seen.append(envelope)

    client = BusClient(hub.url(), TOKEN, "tests.loud", subscriptions=("percept.*",))
    client.subscribe("percept.*", on_envelope)
    await client.connect()
    try:
        await client.publish("percept.person_seen", {"track_id": "t1"})
        await asyncio.sleep(0.2)
    finally:
        await client.close()
    assert len(seen) == 1


async def _peer(hub: Hub, module_id: str, patterns: list[str]):
    """Open a raw bus connection and complete its handshake."""
    connection = await connect(f"{hub.url()}?token={TOKEN}")
    await connection.send(
        json.dumps({"type": "hello", "module_id": module_id, "subscribe": patterns})
    )
    assert json.loads(await connection.recv())["type"] == "welcome"
    return connection


async def test_a_frame_is_relayed_once_and_never_to_its_sender(hub):
    frame = b"\x00" * 8 + b"jpegbytes"
    viewer = await _peer(hub, "viewer", ["frame.*"])
    camera = await _peer(hub, "camera", ["frame.*"])
    try:
        await camera.send(frame)
        assert await asyncio.wait_for(viewer.recv(), timeout=2.0) == frame
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(viewer.recv(), timeout=0.3)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(camera.recv(), timeout=0.3)
    finally:
        await camera.close()
        await viewer.close()


async def test_a_frame_published_in_the_core_reaches_every_subscriber(hub):
    frame = b"\x00" * 8 + b"localbytes"
    first = await _peer(hub, "first", ["frame.*"])
    second = await _peer(hub, "second", ["frame.*"])
    try:
        await hub.bus.publish_frame(frame)
        assert await asyncio.wait_for(first.recv(), timeout=2.0) == frame
        assert await asyncio.wait_for(second.recv(), timeout=2.0) == frame
    finally:
        await second.close()
        await first.close()


async def test_two_clients_publishing_at_once_are_each_excluded_from_their_own():
    """The echo suppression must follow the connection, not a shared attribute."""
    bus = LocalBus(src="core")

    async def slow(envelope: Envelope) -> None:
        await asyncio.sleep(0.05)

    bus.subscribe("*", slow)  # subscribed first, so the hub dispatches after a yield
    hub = Hub(bus, host="127.0.0.1", port=0, token=TOKEN)
    await hub.start()
    left = await _peer(hub, "left", ["percept.*"])
    right = await _peer(hub, "right", ["percept.*"])
    try:
        await left.send(json.dumps(_envelope_json("left")))
        await right.send(json.dumps(_envelope_json("right")))
        assert json.loads(await asyncio.wait_for(left.recv(), timeout=2.0))["src"] == "right"
        assert json.loads(await asyncio.wait_for(right.recv(), timeout=2.0))["src"] == "left"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(left.recv(), timeout=0.3)
    finally:
        await right.close()
        await left.close()
        await hub.stop()


def _envelope_json(src: str) -> dict:
    return Envelope(kind="percept", topic="percept.person_seen", src=src, data={}).to_dict()


class _FakeConnection:
    """A hub socket that only accepts sends once `release` is set."""

    def __init__(self, *, stalled: bool = False) -> None:
        self.sent: list[str | bytes] = []
        self.release = asyncio.Event()
        if not stalled:
            self.release.set()

    async def send(self, payload: str | bytes) -> None:
        await self.release.wait()
        self.sent.append(payload)


def _attach(hub: Hub, module_id: str, *, stalled: bool = False) -> _Client:
    client = _Client(_FakeConnection(stalled=stalled))
    client.module_id = module_id
    client.patterns = (TOPICS.SCENE_STATE,)
    client.start()
    hub._clients.add(client)
    return client


async def test_a_stalled_client_does_not_block_the_hub(hub):
    slow = _attach(hub, "slow", stalled=True)
    fast = _attach(hub, "fast")
    try:
        for index in range(10):
            await hub.bus.publish(TOPICS.SCENE_STATE, {"index": index}, kind="state")
        await asyncio.sleep(0.05)
        assert len(fast.connection.sent) == 10
        assert fast.connection.sent[-1] == slow.outbox[-1].payload
        assert slow.connection.sent == []
    finally:
        await fast.stop()
        await slow.stop()


async def test_stale_state_snapshots_are_dropped_for_a_stalled_client(hub, caplog):
    slow = _attach(hub, "slow", stalled=True)
    try:
        with caplog.at_level("WARNING"):
            for index in range(SEND_QUEUE_MAX * 4):
                await hub.bus.publish(TOPICS.SCENE_STATE, {"index": index}, kind="state")
        assert len(slow.outbox) <= SEND_QUEUE_MAX
        last = json.loads(slow.outbox[-1].payload)
        assert last["data"]["index"] == SEND_QUEUE_MAX * 4 - 1
        assert any("stale" in record.message for record in caplog.records)
    finally:
        await slow.stop()
