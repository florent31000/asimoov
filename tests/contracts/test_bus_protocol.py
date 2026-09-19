"""`Bus` is a structural protocol: any object with the right methods satisfies it."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from asimoov.contracts.bus import Bus, Subscription
from asimoov.contracts.envelope import Envelope


class _Sub:
    def __init__(self, unsubscribe_fn) -> None:
        self._unsubscribe_fn = unsubscribe_fn
        self.unsubscribed = False

    def unsubscribe(self) -> None:
        self.unsubscribed = True
        self._unsubscribe_fn()


class InMemoryBusStub:
    """Minimal in-memory `Bus` implementation, just enough to exercise the protocol."""

    def __init__(self) -> None:
        self._handlers: dict[str, list] = {}
        self._latest: dict[str, Envelope] = {}

    def _matching_handlers(self, topic: str):
        for pattern, handlers in self._handlers.items():
            if pattern == topic or (pattern.endswith("*") and topic.startswith(pattern[:-1])):
                yield from handlers

    async def publish(
        self,
        topic: str,
        data: dict[str, Any],
        *,
        kind: str = "percept",
        corr: str | None = None,
    ) -> None:
        envelope = Envelope(kind=kind, topic=topic, src="stub", data=dict(data), corr=corr)
        if kind == "state":
            self._latest[topic] = envelope
        for handler in list(self._matching_handlers(topic)):
            await handler(envelope)

    def subscribe(self, pattern: str, handler) -> Subscription:
        self._handlers.setdefault(pattern, []).append(handler)

        def _unsubscribe() -> None:
            self._handlers[pattern].remove(handler)

        return _Sub(_unsubscribe)

    async def request(self, topic: str, data: dict[str, Any], *, timeout_s: float) -> Envelope:
        reply: asyncio.Future[Envelope] = asyncio.get_running_loop().create_future()

        async def _on_reply(envelope: Envelope) -> None:
            if not reply.done():
                reply.set_result(envelope)

        sub = self.subscribe(f"{topic}.reply", _on_reply)
        try:
            await self.publish(topic, data, kind="cmd")
            return await asyncio.wait_for(reply, timeout=timeout_s)
        finally:
            sub.unsubscribe()

    def latest(self, topic: str) -> Envelope | None:
        return self._latest.get(topic)


def test_in_memory_stub_satisfies_bus_protocol() -> None:
    assert isinstance(InMemoryBusStub(), Bus)


@pytest.mark.asyncio
async def test_publish_and_subscribe_exact_topic() -> None:
    bus: Bus = InMemoryBusStub()
    received: list[Envelope] = []

    async def handler(envelope: Envelope) -> None:
        received.append(envelope)

    sub = bus.subscribe("body.health", handler)
    await bus.publish("body.health", {"connected": True})

    assert len(received) == 1
    assert received[0].topic == "body.health"
    sub.unsubscribe()

    await bus.publish("body.health", {"connected": False})
    assert len(received) == 1  # unsubscribed, no further deliveries


@pytest.mark.asyncio
async def test_subscribe_wildcard_prefix() -> None:
    bus: Bus = InMemoryBusStub()
    received: list[str] = []

    async def handler(envelope: Envelope) -> None:
        received.append(envelope.topic)

    bus.subscribe("percept.*", handler)
    await bus.publish("percept.person_seen", {})
    await bus.publish("percept.touched", {})
    await bus.publish("body.health", {})

    assert received == ["percept.person_seen", "percept.touched"]


@pytest.mark.asyncio
async def test_request_times_out_without_a_reply() -> None:
    bus: Bus = InMemoryBusStub()
    with pytest.raises(asyncio.TimeoutError):
        await bus.request("body.cmd", {"name": "wave_hello"}, timeout_s=0.05)


@pytest.mark.asyncio
async def test_request_resolves_on_reply() -> None:
    bus: Bus = InMemoryBusStub()

    async def responder(envelope: Envelope) -> None:
        await bus.publish("body.cmd.reply", {"ok": True}, kind="reply", corr=envelope.id)

    bus.subscribe("body.cmd", responder)
    reply = await bus.request("body.cmd", {"name": "wave_hello"}, timeout_s=1.0)
    assert reply.data == {"ok": True}


def test_latest_returns_none_before_any_state_publish() -> None:
    bus: Bus = InMemoryBusStub()
    assert bus.latest("scene.state") is None


@pytest.mark.asyncio
async def test_latest_returns_last_state_envelope() -> None:
    bus: Bus = InMemoryBusStub()
    await bus.publish("scene.state", {"n": 1}, kind="state")
    await bus.publish("scene.state", {"n": 2}, kind="state")

    latest = bus.latest("scene.state")
    assert latest is not None
    assert latest.data == {"n": 2}
