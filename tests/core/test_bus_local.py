"""LocalBus: pattern matching, latest-value state, request/reply, unsubscribe."""

from __future__ import annotations

import asyncio

import pytest

from asimoov.contracts.bus import Bus
from asimoov.contracts.envelope import Envelope
from asimoov.contracts.vocab import TOPICS
from asimoov.core.bus.local import LocalBus, topic_matches


def test_local_bus_satisfies_the_bus_protocol():
    assert isinstance(LocalBus(), Bus)


@pytest.mark.parametrize(
    ("pattern", "topic", "expected"),
    [
        ("percept.*", "percept.person_seen", True),
        ("percept.*", "percept.", True),
        ("percept.*", "voice.event", False),
        ("*", "anything.at.all", True),
        ("body.health", "body.health", True),
        ("body.health", "body.health.extra", False),
    ],
)
def test_topic_matches(pattern, topic, expected):
    assert topic_matches(pattern, topic) is expected


async def test_subscribers_receive_matching_envelopes():
    bus = LocalBus(src="test")
    seen: list[Envelope] = []

    async def handler(envelope: Envelope) -> None:
        seen.append(envelope)

    bus.subscribe("percept.*", handler)
    await bus.publish("percept.person_seen", {"track_id": "t1"})
    await bus.publish("voice.event", {"type": "ignored"})

    assert [envelope.topic for envelope in seen] == ["percept.person_seen"]
    assert seen[0].src == "test"


async def test_only_state_envelopes_are_retained():
    bus = LocalBus()
    await bus.publish("percept.battery", {"level": 0.5})
    await bus.publish(TOPICS.SCENE_STATE, {"people": []}, kind="state")

    assert bus.latest("percept.battery") is None
    assert bus.latest(TOPICS.SCENE_STATE) is not None
    assert bus.state_snapshot().keys() == {TOPICS.SCENE_STATE}


async def test_request_gets_the_matching_reply():
    bus = LocalBus()

    async def responder(envelope: Envelope) -> None:
        if envelope.kind == "cmd":
            await bus.publish(
                TOPICS.BODY_REPLY, {"echo": envelope.data}, kind="reply", corr=envelope.id
            )

    bus.subscribe(TOPICS.BODY_CMD, responder)
    reply = await bus.request(TOPICS.BODY_CMD, {"name": "sit"}, timeout_s=1.0)
    assert reply.data["echo"] == {"name": "sit"}


async def test_request_times_out_without_a_reply():
    bus = LocalBus()
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await bus.request(TOPICS.BODY_CMD, {}, timeout_s=0.05)


async def test_unsubscribe_is_idempotent():
    bus = LocalBus()
    calls: list[str] = []

    async def handler(envelope: Envelope) -> None:
        calls.append(envelope.topic)

    subscription = bus.subscribe("*", handler)
    await bus.publish("percept.battery", {"level": 1.0})
    subscription.unsubscribe()
    subscription.unsubscribe()
    await bus.publish("percept.battery", {"level": 0.9})
    assert calls == ["percept.battery"]


async def test_a_failing_handler_does_not_stop_the_others(caplog):
    bus = LocalBus()
    delivered: list[str] = []

    async def broken(envelope: Envelope) -> None:
        raise RuntimeError("boom")

    async def fine(envelope: Envelope) -> None:
        delivered.append(envelope.topic)

    bus.subscribe("*", broken)
    bus.subscribe("*", fine)
    await bus.publish("percept.battery", {"level": 1.0})
    assert delivered == ["percept.battery"]
