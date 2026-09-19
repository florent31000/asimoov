"""`Hub`: one WebSocket server on port 7331 for the bus and the face page.

Remote modules (perception, a body on another machine, the face page)
connect to ``ws://host:7331/bus?token=...``, send a ``hello`` naming their
module id and subscriptions, and then exchange envelope.v1 JSON. Binary
frames are passed through between clients and never enter the local bus
(plan.md section 4.3). Every other HTTP path is handed to the
`ProcessRequestHook` WS6 installs, so the face page is served from the same
port (plan.md section 4.1).
"""

from __future__ import annotations

import asyncio
import contextlib
import http
import inspect
import json
import logging
import secrets
from collections import deque
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from asimoov.contracts.envelope import Envelope
from asimoov.contracts.face import ProcessRequestHook
from asimoov.core.bus.local import LocalBus, topic_matches
from asimoov.core.config import asimoov_home
from asimoov.core.timeouts import run_with_timeout

log = logging.getLogger(__name__)

BUS_PATH = "/bus"
HELLO_TIMEOUT_S = 5.0
TOKEN_BYTES = 24
#: Owner read/write only. A shared secret on a multi-user machine.
TOKEN_FILE_MODE = 0o600
SEND_QUEUE_MAX = 64

WebSocketRoute = Callable[..., Awaitable[None]]


def read_or_create_token(path=None) -> str:
    """Return the hub token from `~/.asimoov/token`, creating it if absent.

    The file is created readable by its owner only where the OS supports
    it; on Windows `os.chmod` cannot express that and the call is a no-op,
    which is why the mode is applied and not asserted.
    """
    token_path = path or asimoov_home() / "token"
    if token_path.is_file():
        existing = token_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(TOKEN_BYTES)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        token_path.chmod(TOKEN_FILE_MODE)
    return token


@dataclass(frozen=True)
class _Outgoing:
    payload: str | bytes
    kind: str
    topic: str


class _Client:
    """One hub peer, with a bounded outbox drained by its own task.

    The hub's receive and publish paths only ever append to `outbox`, so a
    peer whose socket has stopped draining slows down nobody but itself.
    """

    def __init__(self, connection: ServerConnection) -> None:
        self.connection = connection
        self.module_id = "unknown"
        self.patterns: tuple[str, ...] = ()
        self.outbox: deque[_Outgoing] = deque()
        self._wakeup = asyncio.Event()
        self._closed = False
        self._sender: asyncio.Task[None] | None = None

    def wants(self, topic: str) -> bool:
        return any(topic_matches(pattern, topic) for pattern in self.patterns)

    def wants_frames(self) -> bool:
        return any(topic_matches(pattern, "frame.") for pattern in self.patterns)

    def start(self) -> None:
        self._sender = asyncio.create_task(self._drain(), name=f"hub-send:{self.module_id}")

    async def stop(self) -> None:
        self._closed = True
        self._wakeup.set()
        if self._sender is not None:
            self._sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sender
            self._sender = None

    def enqueue(self, payload: str | bytes, *, kind: str = "", topic: str = "") -> None:
        if self._closed:
            return
        if len(self.outbox) >= SEND_QUEUE_MAX and not self._reclaim(kind, topic):
            log.warning(
                "hub outbox full for %s (%d queued), dropping a %s message",
                self.module_id,
                len(self.outbox),
                kind or "frame",
            )
            return
        self.outbox.append(_Outgoing(payload, kind, topic))
        self._wakeup.set()

    def _reclaim(self, kind: str, topic: str) -> int:
        """Drop the queued `state` snapshots an incoming one supersedes."""
        if kind != "state":
            return 0
        stale = [item for item in self.outbox if item.kind == "state" and item.topic == topic]
        for item in stale:
            self.outbox.remove(item)
        if stale:
            log.warning(
                "hub dropped %d stale %s snapshots for slow client %s",
                len(stale),
                topic,
                self.module_id,
            )
        return len(stale)

    async def _drain(self) -> None:
        try:
            while True:
                item = await self._next()
                if item is None:
                    return
                await self.connection.send(item.payload)
        except ConnectionClosed:
            self._closed = True

    async def _next(self) -> _Outgoing | None:
        while not self.outbox:
            if self._closed:
                return None
            self._wakeup.clear()
            await self._wakeup.wait()
        return self.outbox.popleft()


# The origin of the message being dispatched, per task: two connections are
# served concurrently, so it cannot live on the hub.
_envelope_origin: ContextVar[tuple[_Client, str] | None] = ContextVar(
    "asimoov_hub_envelope_origin", default=None
)
_frame_origin: ContextVar[_Client | None] = ContextVar("asimoov_hub_frame_origin", default=None)


class Hub:
    """WebSocket bus hub plus the HTTP entry point for the face page."""

    def __init__(
        self,
        bus: LocalBus,
        *,
        host: str = "127.0.0.1",
        port: int = 7331,
        token: str | None = None,
        process_request: ProcessRequestHook | None = None,
    ) -> None:
        self.bus = bus
        self.host = host
        self.port = port
        self.token = token or read_or_create_token()
        self.process_request_hook = process_request
        self._server: Server | None = None
        self._clients: set[_Client] = set()
        self._subscription = None
        self._frame_subscription = None
        self._routes: dict[str, WebSocketRoute] = {}

    @property
    def bound_port(self) -> int:
        if self._server is None:
            return self.port
        return self._server.sockets[0].getsockname()[1]

    def url(self) -> str:
        return f"ws://{self.host}:{self.bound_port}{BUS_PATH}"

    def set_process_request_hook(self, hook: ProcessRequestHook | None) -> None:
        """Install the face page's HTTP hook (WS6 calls this through the runtime)."""
        self.process_request_hook = hook

    def route(self, path: str, handler: WebSocketRoute) -> None:
        """Hand WebSocket connections on ``path`` to ``handler(connection, path=...)``.

        Used by the face page (``/face/ws``), which owns its own token check
        and its own protocol: the hub only gates ``/bus``.
        """
        self._routes[path] = handler

    async def start(self) -> None:
        self._subscription = self.bus.subscribe("*", self._on_local_envelope)
        self._frame_subscription = self.bus.subscribe_frames(self._on_local_frame)
        self._server = await serve(
            self._handle_client,
            self.host,
            self.port,
            process_request=self._process_request,
        )
        log.info("hub listening on %s", self.url())

    async def stop(self) -> None:
        if self._subscription is not None:
            self._subscription.unsubscribe()
            self._subscription = None
        if self._frame_subscription is not None:
            self._frame_subscription.unsubscribe()
            self._frame_subscription = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._clients.clear()

    # -- HTTP -------------------------------------------------------------

    async def _process_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        split = urlsplit(request.path)
        if split.path in self._routes:
            return None
        if split.path == BUS_PATH:
            token = parse_qs(split.query).get("token", [""])[0]
            if not secrets.compare_digest(token, self.token):
                log.warning("hub rejected a connection with an invalid token")
                return connection.respond(http.HTTPStatus.UNAUTHORIZED, "invalid token\n")
            return None

        hook = self.process_request_hook
        if hook is not None:
            result = hook(request.path)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                headers = Headers(list(result.headers))
                # Without a length the client waits for EOF and the
                # connection lingers until the close timeout.
                if "Content-Length" not in headers:
                    headers["Content-Length"] = str(len(result.body))
                return Response(
                    result.status,
                    http.HTTPStatus(result.status).phrase,
                    headers,
                    result.body,
                )
        return connection.respond(http.HTTPStatus.NOT_FOUND, "not found\n")

    # -- WebSocket --------------------------------------------------------

    async def _handle_client(self, connection: ServerConnection) -> None:
        path = connection.request.path if connection.request is not None else BUS_PATH
        route = self._routes.get(urlsplit(path).path)
        if route is not None:
            await route(connection, path=path)
            return

        client = _Client(connection)
        try:
            if not await self._handshake(client):
                return
        except (TimeoutError, asyncio.TimeoutError):
            await connection.close(code=1002, reason="hello timeout")
            return
        except ConnectionClosed:
            return

        self._clients.add(client)
        client.start()
        log.info("hub client connected: %s (%s)", client.module_id, ", ".join(client.patterns))
        try:
            async for message in connection:
                if isinstance(message, bytes):
                    await self._deliver_frame(message, origin=client)
                else:
                    await self._on_client_message(client, message)
        except ConnectionClosed:
            pass
        finally:
            self._clients.discard(client)
            await client.stop()
            log.info("hub client disconnected: %s", client.module_id)

    async def _handshake(self, client: _Client) -> bool:
        # `run_with_timeout`, not `wait_for`: the caller catches
        # `TimeoutError` to reject a silent client, and `wait_for` would
        # report the hub's own shutdown as one.
        answered, raw = await run_with_timeout(client.connection.recv(), HELLO_TIMEOUT_S)
        if not answered:
            raise TimeoutError(f"no hello within {HELLO_TIMEOUT_S}s")
        try:
            hello = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            await client.connection.close(code=1002, reason="hello must be JSON")
            return False
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            await client.connection.close(code=1002, reason="first message must be a hello")
            return False

        client.module_id = str(hello.get("module_id") or "unknown")
        client.patterns = tuple(hello.get("subscribe") or ())
        await client.connection.send(
            json.dumps({"type": "welcome", "module_id": client.module_id, "v": 1})
        )
        for topic, envelope in self.bus.state_snapshot().items():
            if client.wants(topic):
                await client.connection.send(json.dumps(envelope.to_dict()))
        return True

    async def _on_client_message(self, client: _Client, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            log.warning("hub client %s sent invalid JSON", client.module_id)
            return
        if not isinstance(payload, dict):
            log.warning("hub client %s sent a non-object message", client.module_id)
            return

        if payload.get("type") == "subscribe":
            client.patterns = tuple(payload.get("patterns") or ())
            return

        try:
            envelope = Envelope.from_dict(payload)
        except (KeyError, ValueError) as exc:
            log.warning("hub client %s sent an invalid envelope: %s", client.module_id, exc)
            return

        token = _envelope_origin.set((client, envelope.id))
        try:
            await self.bus.publish_envelope(envelope)
        finally:
            _envelope_origin.reset(token)

    async def _deliver_frame(self, frame: bytes, *, origin: _Client) -> None:
        """Hand a client's frame to the local bus, which relays it once.

        The hub is itself a frame subscriber, so `_on_local_frame` does the
        relaying for both paths; it needs the origin to skip the sender.
        """
        token = _frame_origin.set(origin)
        try:
            await self.bus.deliver_frame(frame)
        finally:
            _frame_origin.reset(token)

    async def _on_local_envelope(self, envelope: Envelope) -> None:
        if envelope.kind == "frame":
            return
        payload = json.dumps(envelope.to_dict())
        # Never echo a client its own envelope -- but only that envelope. A
        # `reply` a local responder publishes while the `cmd` is still being
        # dispatched is a different envelope, and it is exactly what the
        # client that sent the `cmd` is waiting for.
        origin = _envelope_origin.get()
        sender = origin[0] if origin is not None and origin[1] == envelope.id else None
        for client in list(self._clients):
            if client is sender or not client.wants(envelope.topic):
                continue
            client.enqueue(payload, kind=envelope.kind, topic=envelope.topic)

    async def _on_local_frame(self, frame: bytes) -> None:
        """Relay the bytes untouched: the hub never decodes a `frame.*` message."""
        origin = _frame_origin.get()
        for client in list(self._clients):
            if client is origin or not client.wants_frames():
                continue
            client.enqueue(frame, kind="frame")
