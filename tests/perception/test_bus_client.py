"""`core.bus.BusClient` -- perception's link to the hub -- against a real server.

The fake hub below speaks the protocol `core/bus/hub.py` serves: a bare
``hello`` answered by a ``welcome``, then envelope.v1 JSON text messages and
binary `contracts.frames` messages.
"""

from __future__ import annotations

import asyncio

import pytest
from helpers import FakeHub, deadline

from asimoov.contracts.bus import Bus
from asimoov.contracts.envelope import Envelope
from asimoov.contracts.frames import encode_frame
from asimoov.core.bus.client import BusClient, hub_url
from asimoov.core.bus.local import topic_matches


def test_the_client_satisfies_the_bus_protocol():
    assert isinstance(BusClient("ws://127.0.0.1:7331"), Bus)


def test_the_bus_path_is_appended():
    assert hub_url("ws://host:7331") == "ws://host:7331/bus"
    assert hub_url("wss://host/bus") == "wss://host/bus"
    assert hub_url("ws://host:7331", token="abc") == "ws://host:7331/bus?token=abc"


def test_a_non_websocket_scheme_is_refused():
    with pytest.raises(ValueError, match="ws://"):
        hub_url("http://host:7331")


@pytest.mark.parametrize(
    ("pattern", "topic", "expected"),
    [
        ("percept.*", "percept.person_seen", True),
        ("percept.*", "frame.browser", False),
        ("body.health", "body.health", True),
        ("body.health", "body.healthy", False),
        ("*", "anything", True),
    ],
)
def test_topic_matching(pattern: str, topic: str, expected: bool):
    assert topic_matches(pattern, topic) is expected


async def test_hello_handshake_announces_the_module_and_its_subscriptions():
    async with FakeHub() as hub:
        client = BusClient(hub.url, module_id="perception")
        client.subscribe("perception.*", lambda envelope: asyncio.sleep(0))
        await client.connect(2.0)
        await client.close()
    assert hub.hello == {
        "type": "hello",
        "module_id": "perception",
        "subscribe": ["perception.*"],
    }


async def test_a_hub_that_never_welcomes_is_not_considered_connected():
    async with FakeHub(welcome=False) as hub:
        client = BusClient(hub.url, reconnect_min_s=5.0)
        with pytest.raises(asyncio.TimeoutError):
            await client.connect(0.5)
        assert client.connected is False
        await client.close()


async def test_a_frame_handler_subscribes_to_the_frame_topics():
    async with FakeHub() as hub:
        client = BusClient(hub.url)
        client.subscribe_frames(lambda payload: asyncio.sleep(0))
        await client.connect(2.0)
        await client.close()
    assert hub.hello["subscribe"] == ["frame.*"]


async def test_a_late_subscription_is_announced_to_the_hub():
    async with FakeHub() as hub:
        client = BusClient(hub.url)
        await client.connect(2.0)
        client.subscribe("percept.*", lambda envelope: asyncio.sleep(0))
        await hub.wait_for_patterns(("percept.*",))
        await client.close()


async def test_the_token_is_sent_as_a_query_parameter():
    async with FakeHub() as hub:
        client = BusClient(hub.url, token="s3cret")
        await client.connect(2.0)
        await client.close()
    assert hub.paths[0] == "/bus?token=s3cret"


async def test_published_percepts_conform_to_the_envelope_schema(envelope_validator):
    async with FakeHub() as hub:
        client = BusClient(hub.url, module_id="perception.face_id")
        await client.connect(2.0)
        await client.publish("percept.person_seen", {"type": "person_seen"})
        await hub.wait_for(1)
        await client.close()
    published = hub.received[0]
    envelope_validator.validate(published)
    assert published["kind"] == "percept"
    assert published["src"] == "perception.face_id"
    assert published["topic"] == "percept.person_seen"


async def test_subscribed_handlers_receive_matching_envelopes():
    seen: list[Envelope] = []

    async def handler(envelope: Envelope) -> None:
        seen.append(envelope)

    async with FakeHub() as hub:
        client = BusClient(hub.url)
        client.subscribe("perception.*", handler)
        await client.connect(2.0)
        await hub.push(Envelope(kind="cmd", topic="perception.face_id.enroll", src="core"))
        await hub.push(Envelope(kind="cmd", topic="body.cmd", src="core"))
        async with deadline(2.0):
            while not seen:
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        await client.close()
    assert [envelope.topic for envelope in seen] == ["perception.face_id.enroll"]


async def test_latest_keeps_the_last_state_per_topic():
    async with FakeHub() as hub:
        client = BusClient(hub.url)
        await client.connect(2.0)
        await hub.push(Envelope(kind="state", topic="scene.state", src="core", data={"n": 1}))
        await hub.push(Envelope(kind="state", topic="scene.state", src="core", data={"n": 2}))
        async with deadline(2.0):
            while client.latest("scene.state") is None:
                await asyncio.sleep(0.01)
            while client.latest("scene.state").data["n"] != 2:
                await asyncio.sleep(0.01)
        await client.close()


async def test_a_reply_resolves_the_matching_request():
    async def responder(client: BusClient, hub: FakeHub) -> None:
        await hub.wait_for(1)
        cmd = hub.received[0]
        await hub.push(
            Envelope(kind="reply", topic=cmd["topic"], src="core", data={"ok": True}, corr=cmd["id"])
        )

    async with FakeHub() as hub:
        client = BusClient(hub.url)
        await client.connect(2.0)
        task = asyncio.create_task(responder(client, hub))
        reply = await client.request("body.cmd", {"name": "wave"}, timeout_s=2.0)
        await task
        await client.close()
    assert reply.data == {"ok": True}


async def test_a_request_without_a_reply_times_out():
    async with FakeHub() as hub:
        client = BusClient(hub.url)
        await client.connect(2.0)
        with pytest.raises(asyncio.TimeoutError):
            await client.request("body.cmd", {}, timeout_s=0.2)
        await client.close()


async def test_reply_correlates_to_the_command_id():
    async with FakeHub() as hub:
        client = BusClient(hub.url, module_id="perception")
        await client.connect(2.0)
        cmd = Envelope(kind="cmd", topic="perception.face_id.enroll", src="core")
        await client.reply(cmd, {"ok": True, "samples": 5})
        await hub.wait_for(1)
        await client.close()
    sent = hub.received[0]
    assert sent["kind"] == "reply"
    assert sent["corr"] == cmd.id
    assert sent["topic"] == "perception.face_id.enroll"


async def test_binary_frames_go_to_the_frame_handler_not_to_subscriptions():
    frames: list[bytes] = []
    envelopes: list[Envelope] = []

    async def on_frame(payload: bytes) -> None:
        frames.append(payload)

    async def handler(envelope: Envelope) -> None:
        envelopes.append(envelope)

    async with FakeHub() as hub:
        client = BusClient(hub.url)
        client.subscribe("frame.*", handler)
        client.subscribe_frames(on_frame)
        await client.connect(2.0)
        await hub.push_bytes(encode_frame("frame.browser", b"\xff\xd8\xff-jpeg"))
        async with deadline(2.0):
            while not frames:
                await asyncio.sleep(0.01)
        await client.close()
    assert envelopes == []


async def test_publishing_while_disconnected_is_counted_not_queued():
    client = BusClient("ws://127.0.0.1:1/bus")
    await client.publish("percept.person_seen", {"type": "person_seen"})
    assert client.dropped_publishes == 1
    assert client.connected is False


async def test_a_malformed_message_does_not_kill_the_connection():
    seen: list[Envelope] = []

    async def handler(envelope: Envelope) -> None:
        seen.append(envelope)

    async with FakeHub() as hub:
        client = BusClient(hub.url)
        client.subscribe("percept.*", handler)
        await client.connect(2.0)
        await hub.connections[-1].send("not json at all")
        await hub.push(Envelope(kind="percept", topic="percept.battery", src="body"))
        async with deadline(2.0):
            while not seen:
                await asyncio.sleep(0.01)
        await client.close()
    assert seen[0].topic == "percept.battery"


async def test_unsubscribing_stops_delivery():
    seen: list[Envelope] = []

    async def handler(envelope: Envelope) -> None:
        seen.append(envelope)

    async with FakeHub() as hub:
        client = BusClient(hub.url)
        subscription = client.subscribe("percept.*", handler)
        await client.connect(2.0)
        subscription.unsubscribe()
        subscription.unsubscribe()  # idempotent
        await hub.push(Envelope(kind="percept", topic="percept.battery", src="body"))
        await asyncio.sleep(0.1)
        await client.close()
    assert seen == []


async def test_stop_is_idempotent():
    async with FakeHub() as hub:
        client = BusClient(hub.url)
        await client.connect(2.0)
        await client.close()
        await client.close()
