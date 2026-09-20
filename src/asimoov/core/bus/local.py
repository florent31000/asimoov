"""`LocalBus`: the in-process implementation of `contracts.bus.Bus`.

Handlers are awaited in subscription order inside `publish`, so a reply
published by a responder is already delivered by the time `request`
awaits its future. Slow handlers therefore slow their publisher down on
purpose: a component that needs to take its time spawns its own task.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from asimoov.contracts.bus import EnvelopeHandler
from asimoov.contracts.envelope import Envelope
from asimoov.core.bus.transport import InboundFrameHandler, Transport
from asimoov.core.timeouts import run_with_timeout

log = logging.getLogger(__name__)


def topic_matches(pattern: str, topic: str) -> bool:
    """Match a topic against a `Bus.subscribe` pattern.

    An exact topic matches itself; a pattern ending in ``*`` matches every
    topic sharing its prefix (``"percept.*"`` matches ``percept.person_seen``
    and the bare ``percept.`` topic; ``"*"`` matches everything). No other
    wildcard form exists.
    """
    if pattern.endswith("*"):
        return topic.startswith(pattern[:-1])
    return pattern == topic


class Subscription:
    """Handle returned by `LocalBus.subscribe`."""

    def __init__(self, bus: LocalBus, pattern: str, handler: EnvelopeHandler) -> None:
        self._bus = bus
        self._entry: tuple[str, EnvelopeHandler] | None = (pattern, handler)

    def unsubscribe(self) -> None:
        if self._entry is not None:
            self._bus._remove(self._entry)
            self._entry = None


class FrameSubscription:
    """Handle returned by `LocalBus.subscribe_frames`."""

    def __init__(self, bus: LocalBus, handler: InboundFrameHandler) -> None:
        self._bus = bus
        self._handler: InboundFrameHandler | None = handler

    def unsubscribe(self) -> None:
        if self._handler is not None:
            self._bus._remove_frame_handler(self._handler)
            self._handler = None


class LocalBus:
    """In-process publish/subscribe/request bus, optionally bridged by a `Transport`."""

    def __init__(self, src: str = "core", transport: Transport | None = None) -> None:
        self.src = src
        self._subscriptions: list[tuple[str, EnvelopeHandler]] = []
        self._frame_handlers: list[InboundFrameHandler] = []
        self._latest: dict[str, Envelope] = {}
        self._waiters: dict[str, asyncio.Future[Envelope]] = {}
        self._transport = transport

    async def start(self) -> None:
        """Start the attached transport, if any."""
        if self._transport is not None:
            self._transport.set_frame_handler(self.deliver_frame)
            await self._transport.start(self._inbound)

    async def stop(self) -> None:
        """Stop the transport and fail every pending `request`."""
        if self._transport is not None:
            await self._transport.stop()
        for future in self._waiters.values():
            if not future.done():
                future.cancel()
        self._waiters.clear()

    def attach_transport(self, transport: Transport) -> None:
        self._transport = transport

    # -- contracts.bus.Bus ------------------------------------------------

    async def publish(
        self,
        topic: str,
        data: Mapping[str, Any],
        *,
        kind: str = "percept",
        corr: str | None = None,
    ) -> None:
        envelope = Envelope(kind=kind, topic=topic, src=self.src, data=dict(data), corr=corr)
        await self.publish_envelope(envelope)

    def subscribe(self, pattern: str, handler: EnvelopeHandler) -> Subscription:
        entry = (pattern, handler)
        self._subscriptions.append(entry)
        return Subscription(self, pattern, handler)

    async def request(
        self, topic: str, data: Mapping[str, Any], *, timeout_s: float
    ) -> Envelope:
        envelope = Envelope(kind="cmd", topic=topic, src=self.src, data=dict(data))
        future: asyncio.Future[Envelope] = asyncio.get_running_loop().create_future()
        self._waiters[envelope.id] = future
        try:
            await self.publish_envelope(envelope)
            # `run_with_timeout`, not `wait_for`: a caller inside a supervised
            # loop that catches TimeoutError would otherwise swallow its own
            # cancellation and keep running (see `core/timeouts.py`).
            answered, reply = await run_with_timeout(future, timeout_s)
        finally:
            self._waiters.pop(envelope.id, None)
        if not answered:
            raise asyncio.TimeoutError(f"no reply to {topic!r} within {timeout_s}s")
        return reply

    def latest(self, topic: str) -> Envelope | None:
        return self._latest.get(topic)

    # -- binary frames ----------------------------------------------------

    def subscribe_frames(self, handler: InboundFrameHandler) -> FrameSubscription:
        """Receive every binary ``frame.*`` message, local or from the far side.

        Frames are not envelopes: they carry their own topic in their header
        (`contracts.frames`) and never enter the envelope dispatch.
        """
        self._frame_handlers.append(handler)
        return FrameSubscription(self, handler)

    async def publish_frame(self, frame: bytes) -> None:
        """Publish an encoded binary frame locally and over the transport."""
        await self._dispatch_frame(frame)
        if self._transport is not None:
            await self._transport.send_frame(frame)

    async def deliver_frame(self, frame: bytes) -> None:
        """Dispatch a frame received from the far side, without sending it back."""
        await self._dispatch_frame(frame)

    async def _dispatch_frame(self, frame: bytes) -> None:
        for handler in list(self._frame_handlers):
            try:
                await handler(frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("bus frame handler failed (%d bytes)", len(frame))

    # -- internals --------------------------------------------------------

    async def publish_envelope(self, envelope: Envelope, *, forward: bool = True) -> None:
        """Dispatch an already-built envelope, optionally forwarding it outward."""
        if envelope.kind == "state":
            self._latest[envelope.topic] = envelope
        if envelope.kind == "reply" and envelope.corr:
            waiter = self._waiters.get(envelope.corr)
            if waiter is not None and not waiter.done():
                waiter.set_result(envelope)
        for pattern, handler in list(self._subscriptions):
            if not topic_matches(pattern, envelope.topic):
                continue
            try:
                await handler(envelope)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("bus handler failed for topic %s (pattern %s)", envelope.topic, pattern)
        if forward and self._transport is not None:
            await self._transport.send(envelope)

    async def _inbound(self, envelope: Envelope) -> None:
        await self.publish_envelope(envelope, forward=False)

    def state_snapshot(self) -> dict[str, Envelope]:
        """Every retained latest-value ``state`` envelope, by topic."""
        return dict(self._latest)

    def _remove(self, entry: tuple[str, EnvelopeHandler]) -> None:
        if entry in self._subscriptions:
            self._subscriptions.remove(entry)

    def _remove_frame_handler(self, handler: InboundFrameHandler) -> None:
        if handler in self._frame_handlers:
            self._frame_handlers.remove(handler)
