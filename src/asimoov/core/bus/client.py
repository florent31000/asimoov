"""`BusClient`: the bus as seen by a module in another process or machine.

Wraps a `LocalBus` whose `Transport` is a WebSocket connection to the hub,
so perception (`python -m asimoov.perception`), a remote body or the face
page program against exactly the same `contracts.bus.Bus` surface as
in-process components.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from asimoov.contracts.bus import EnvelopeHandler
from asimoov.contracts.envelope import Envelope
from asimoov.core.bus.local import FrameSubscription, LocalBus, Subscription
from asimoov.core.bus.transport import InboundFrameHandler, InboundHandler, Transport
from asimoov.core.config import asimoov_home

log = logging.getLogger(__name__)

RECONNECT_MIN_S = 0.5
RECONNECT_MAX_S = 5.0
CONNECT_TIMEOUT_S = 10.0
FRAME_PATTERN = "frame.*"
BUS_PATH = "/bus"
DEFAULT_HUB_URL = "ws://127.0.0.1:7331/bus"


def hub_url(base: str, *, token: str | None = None) -> str:
    """Normalize ``ws://host:7331`` into the full bus URL, token query included.

    Raises:
        ValueError: if the scheme is not ``ws`` or ``wss``.
    """
    parts = urlsplit(base)
    if parts.scheme not in ("ws", "wss"):
        raise ValueError(f"hub URL must be ws:// or wss://, got {base!r}")
    path = parts.path if parts.path not in ("", "/") else BUS_PATH
    query = parts.query
    if token:
        query = "&".join(filter(None, [query, urlencode({"token": token})]))
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def read_token() -> str | None:
    """Read the shared hub token the core wrote, or None if there is none."""
    path = asimoov_home() / "token"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip() or None


class WebSocketTransport(Transport):
    """Envelope and frame link to the hub, with hello handshake and reconnection."""

    def __init__(
        self,
        url: str,
        token: str | None,
        module_id: str,
        patterns: tuple[str, ...],
        *,
        reconnect_min_s: float = RECONNECT_MIN_S,
        reconnect_max_s: float = RECONNECT_MAX_S,
    ) -> None:
        self.url = hub_url(url, token=token)
        self.module_id = module_id
        self.patterns = patterns
        self.reconnect_min_s = reconnect_min_s
        self.reconnect_max_s = reconnect_max_s
        self.dropped = 0
        self._connection: ClientConnection | None = None
        self._task: asyncio.Task | None = None
        self._inbound: InboundHandler | None = None
        self._inbound_frame: InboundFrameHandler | None = None
        self._closing = False
        self.connected = asyncio.Event()

    def _uri(self) -> str:
        return self.url

    async def start(self, inbound: InboundHandler) -> None:
        """Start connecting. Returns as soon as the task is running.

        `BusClient.connect` is the one that waits for the handshake, so a
        module can choose to run degraded while the hub comes up.
        """
        self._inbound = inbound
        self._closing = False
        self._task = asyncio.create_task(self._run(), name=f"bus-client:{self.module_id}")

    async def wait_connected(self, timeout_s: float = CONNECT_TIMEOUT_S) -> None:
        """Wait for the ``welcome``.

        Raises:
            TimeoutError: if the hub does not answer in time.
        """
        await asyncio.wait_for(self.connected.wait(), timeout=timeout_s)

    async def stop(self) -> None:
        self._closing = True
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.connected.clear()

    async def send(self, envelope: Envelope) -> None:
        connection = self._connection
        if connection is None:
            self.dropped += 1
            log.warning("bus client %s dropped %s: link is down", self.module_id, envelope.topic)
            return
        try:
            await connection.send(json.dumps(envelope.to_dict()))
        except ConnectionClosed:
            self.dropped += 1
            log.warning("bus client %s dropped %s: link closed", self.module_id, envelope.topic)

    async def send_frame(self, frame: bytes) -> None:
        """Send a binary ``frame.*`` message (`contracts.frames`)."""
        connection = self._connection
        if connection is None:
            self.dropped += 1
            return
        try:
            await connection.send(frame)
        except ConnectionClosed:
            self.dropped += 1
            log.warning("bus client %s dropped a frame: link closed", self.module_id)

    def set_frame_handler(self, handler: InboundFrameHandler | None) -> None:
        self._inbound_frame = handler

    async def update_patterns(self, patterns: tuple[str, ...]) -> None:
        """Tell a live hub about subscriptions taken after the handshake."""
        self.patterns = patterns
        connection = self._connection
        if connection is not None:
            await connection.send(json.dumps({"type": "subscribe", "patterns": list(patterns)}))

    async def _run(self) -> None:
        delay = self.reconnect_min_s
        while not self._closing:
            try:
                async with connect(self._uri(), max_size=None) as connection:
                    self._connection = connection
                    await connection.send(
                        json.dumps(
                            {
                                "type": "hello",
                                "module_id": self.module_id,
                                "subscribe": list(self.patterns),
                            }
                        )
                    )
                    welcome = json.loads(await connection.recv())
                    if welcome.get("type") != "welcome":
                        raise RuntimeError(f"hub did not welcome us: {welcome!r}")
                    delay = self.reconnect_min_s
                    self.connected.set()
                    await self._read_loop(connection)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._closing:
                    return
                log.warning("bus client %s link lost (%s), retrying in %.1fs", self.module_id, exc, delay)
            finally:
                self._connection = None
                self.connected.clear()
            if self._closing:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.reconnect_max_s)

    async def _read_loop(self, connection: ClientConnection) -> None:
        async for message in connection:
            if isinstance(message, bytes):
                if self._inbound_frame is not None:
                    await self._inbound_frame(message)
                continue
            if self._inbound is None:
                continue
            try:
                envelope = Envelope.from_dict(json.loads(message))
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                log.warning("bus client %s received an invalid envelope: %s", self.module_id, exc)
                continue
            await self._inbound(envelope)


class BusClient:
    """A `contracts.bus.Bus` backed by the hub, for out-of-process modules.

    Used by perception (``python -m asimoov.perception``), a remote body and
    anything else living outside the core's process. It carries binary
    ``frame.*`` messages too (`subscribe_frames`, `send_frame`), which is
    what lets a camera on one machine feed a pipeline on another.

    Publishing while the link is down drops the envelope and counts it in
    `dropped_publishes`: percepts are ephemeral, and a queue of stale
    bearings is worse than a gap.
    """

    def __init__(
        self,
        url: str,
        token: str | None = None,
        module_id: str = "module",
        *,
        subscriptions: tuple[str, ...] = (),
        reconnect_min_s: float = RECONNECT_MIN_S,
        reconnect_max_s: float = RECONNECT_MAX_S,
    ) -> None:
        self.module_id = module_id
        self._updates: set[asyncio.Task] = set()
        self._transport = WebSocketTransport(
            url,
            token,
            module_id,
            subscriptions,
            reconnect_min_s=reconnect_min_s,
            reconnect_max_s=reconnect_max_s,
        )
        self._bus = LocalBus(src=module_id, transport=self._transport)

    @property
    def url(self) -> str:
        """The normalized bus URL, token query included."""
        return self._transport.url

    @property
    def connected(self) -> bool:
        return self._transport.connected.is_set()

    @property
    def dropped_publishes(self) -> int:
        return self._transport.dropped

    async def connect(self, timeout_s: float = CONNECT_TIMEOUT_S) -> None:
        """Connect to the hub and complete the ``hello``/``welcome`` handshake.

        Raises:
            TimeoutError: if the hub does not welcome us within ``timeout_s``.
                The client keeps retrying in the background either way.
        """
        await self._bus.start()
        await self._transport.wait_connected(timeout_s)

    async def close(self) -> None:
        """Close the link and stop reconnecting. Idempotent."""
        await self._bus.stop()

    async def publish(
        self,
        topic: str,
        data: Mapping[str, Any],
        *,
        kind: str = "percept",
        corr: str | None = None,
    ) -> None:
        await self._bus.publish(topic, data, kind=kind, corr=corr)

    def subscribe(self, pattern: str, handler: EnvelopeHandler) -> Subscription:
        subscription = self._bus.subscribe(pattern, handler)
        self._announce(pattern)
        return subscription

    async def request(self, topic: str, data: Mapping[str, Any], *, timeout_s: float) -> Envelope:
        """Send a ``cmd`` and wait for its ``reply``.

        The reply comes back on the command's own topic, and the hub only
        forwards what this client is subscribed to -- so the subscription is
        announced first, and awaited rather than scheduled, so it is on the
        wire before the command.
        """
        await self._ensure_pattern(topic)
        return await self._bus.request(topic, data, timeout_s=timeout_s)

    def latest(self, topic: str) -> Envelope | None:
        return self._bus.latest(topic)

    # -- binary frames ----------------------------------------------------

    async def send_frame(self, frame: bytes) -> None:
        await self._transport.send_frame(frame)

    def subscribe_frames(
        self, handler: InboundFrameHandler, *, pattern: str = FRAME_PATTERN
    ) -> FrameSubscription:
        """Receive the hub's binary ``frame.*`` messages.

        ``pattern`` is announced to the hub too: it only relays frames to
        clients whose subscriptions cover ``frame.``.
        """
        subscription = self._bus.subscribe_frames(handler)
        self._announce(pattern)
        return subscription

    async def send(self, envelope: Envelope) -> None:
        """Publish an already-built envelope, keeping its own ``src``.

        One process can host several producers (``perception.face_id``,
        ``perception.vad``) on one socket; the envelope must still name the
        producer, not the process.
        """
        await self._bus.publish_envelope(envelope)

    async def reply(self, cmd: Envelope, payload: Mapping[str, Any]) -> None:
        """Reply to a ``cmd`` envelope on its own topic, correlated by its id."""
        await self.send(
            Envelope(
                kind="reply",
                topic=cmd.topic,
                src=self.module_id,
                data=dict(payload),
                corr=cmd.id,
            )
        )

    def _announce(self, pattern: str) -> None:
        """Add ``pattern`` to what the hub forwards to this client.

        The patterns are recorded synchronously so a subscription taken just
        before `connect` still makes it into the ``hello``; the update is only
        *sent* when there already is a live link.
        """
        patterns = self._with(pattern)
        if patterns is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._updates.add(task := loop.create_task(self._transport.update_patterns(patterns)))
        task.add_done_callback(self._updates.discard)

    async def _ensure_pattern(self, pattern: str) -> None:
        """Like `_announce`, but on the wire before the caller's next message."""
        patterns = self._with(pattern)
        if patterns is not None:
            await self._transport.update_patterns(patterns)

    def _with(self, pattern: str) -> tuple[str, ...] | None:
        """Record ``pattern`` and return the new set, or None if unchanged."""
        patterns = tuple(dict.fromkeys((*self._transport.patterns, pattern)))
        if patterns == self._transport.patterns:
            return None
        self._transport.patterns = patterns
        return patterns
