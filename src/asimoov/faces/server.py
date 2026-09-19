"""Face page server: the `ProcessRequestHook` WS1 mounts, and `WebFace`.

The hub (WS1) owns port 7331. It serves the face page by calling the hook
returned by `make_process_request` before every WebSocket handshake, and
hands every connection on ``/face/ws`` to `WebFace.attach`, which broadcasts
`FaceState` envelopes to the connected pages at up to 20 Hz.

Paths (plan.md section 4.6):

- ``GET /face`` -- 301 to ``/face/``, which serves the page;
- ``GET /face/face.js``, ``/face/face.css``, ``/face/emotions.json`` -- assets;
- ``ws://host:7331/face/ws?token=...`` -- the page's bus connection.

The page and the WebSocket use different paths because the hook is given a
path only: a browser opening ``/face?token=...`` and a WebSocket connecting
to ``/face?token=...`` would otherwise be indistinguishable.

Camera frames the page sends up use the frozen binary codec in
`contracts.frames` (the same one perception and the Go2 use).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import math
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from asimoov.contracts.bus import Bus
from asimoov.contracts.envelope import Envelope
from asimoov.contracts.face import FaceGaze, FaceRenderer, FaceState, ProcessRequestResponse
from asimoov.contracts.frames import decode_frame, encode_frame
from asimoov.contracts.vocab import EMOTIONS, TOPICS
from asimoov.faces.emotions import WEB_DIR

logger = logging.getLogger(__name__)

FACE_PATH = "/face"
FACE_WS_PATH = "/face/ws"
DEFAULT_PORT = 7331
DEFAULT_BROADCAST_HZ = 20.0
SRC = "faces.web"

TOUCHED_TOPIC = TOPICS.PERCEPT_PREFIX + "touched"

_ASSETS: dict[str, str] = {
    "index.html": "text/html; charset=utf-8",
    "face.js": "text/javascript; charset=utf-8",
    "face.css": "text/css; charset=utf-8",
    "emotions.json": "application/json; charset=utf-8",
}

FRAME_BROWSER_TOPIC = TOPICS.FRAME_PREFIX + "browser"
JPEG_CONTENT_TYPE = "image/jpeg"


def encode_frame_header(topic: str, ts_ms: int, *, seq: int = 0) -> bytes:
    """Build the header (and topic) of a binary ``frame.*`` message.

    Kept as a helper because the page sends the header and the JPEG as two
    slices of one buffer; the layout itself is frozen in `contracts.frames`.
    """
    return encode_frame(topic, b"", seq=seq, ts_ms=ts_ms)


def _asset_response(name: str) -> ProcessRequestResponse:
    body = (WEB_DIR / name).read_bytes()
    return ProcessRequestResponse(
        status=200,
        headers=(
            ("Content-Type", _ASSETS[name]),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-cache"),
        ),
        body=body,
    )


def _redirect(location: str) -> ProcessRequestResponse:
    return ProcessRequestResponse(
        status=301, headers=(("Location", location), ("Content-Length", "0"))
    )


def _not_found(path: str) -> ProcessRequestResponse:
    body = f"not found: {path}\n".encode()
    return ProcessRequestResponse(
        status=404,
        headers=(
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ),
        body=body,
    )


def make_process_request(
    *, base_path: str = FACE_PATH
) -> Callable[[str], ProcessRequestResponse | None]:
    """Return the `contracts.face.ProcessRequestHook` the hub mounts.

    The hook answers ``GET <base_path>`` and its assets as plain HTTP, and
    returns None for every other path (including ``<base_path>/ws``) so the
    hub completes the WebSocket handshake itself. ``<base_path>`` redirects
    to ``<base_path>/`` so the page's relative asset URLs resolve under it.
    """
    ws_path = base_path + "/ws"

    def process_request(path: str) -> ProcessRequestResponse | None:
        split = urlsplit(path)
        request_path = split.path
        if request_path == ws_path:
            return None
        if request_path == base_path:
            location = base_path + "/" + (f"?{split.query}" if split.query else "")
            return _redirect(location)
        if request_path in (base_path + "/", base_path + "/index.html"):
            return _asset_response("index.html")
        if request_path.startswith(base_path + "/"):
            name = request_path[len(base_path) + 1 :]
            if name in _ASSETS:
                return _asset_response(name)
            return _not_found(request_path)
        return None

    return process_request


class WebFace(FaceRenderer):
    """Broadcasts `FaceState` to every connected face page, at most 20 Hz.

    ``start(ctx)`` accepts:

    - ``bus``: a `contracts.bus.Bus`, used to publish the ``percept.touched``
      a page tap produces;
    - ``on_estop``: ``Callable[[str], None | Awaitable[None]]`` called when a
      page presses the E-STOP button and there is no bus to publish
      `TOPICS.SAFETY_ESTOP` on. Without either, the press is logged as an
      error, never swallowed;
    - ``on_frame``: ``Callable[[str, int, str, bytes], None]`` receiving
      ``(topic, ts_ms, content_type, jpeg)`` for every binary camera frame a
      page sends, decoded with `contracts.frames` (``ts_ms`` is that codec's
      capture time: Unix milliseconds modulo 2**32). Without it frames are
      dropped and counted;
    - ``token``: if set, a page must connect with a matching ``?token=``.

    Intermediate states are coalesced rather than queued (the renderer
    contract asks for dropped frames, not backlog); the latest one is always
    flushed within one broadcast period, and a state carrying ``blink`` is
    never dropped, since a blink is an edge and not a level.
    """

    ws_path = FACE_WS_PATH

    def __init__(
        self, *, max_hz: float = DEFAULT_BROADCAST_HZ, src: str = SRC, base_path: str = FACE_PATH
    ) -> None:
        # The hub mounts a renderer that serves a page by reading these three
        # off the instance (`Runtime._mount_face`, documented in
        # `contracts.face`): the HTTP hook, the WebSocket path, and `attach`.
        self.process_request = make_process_request(base_path=base_path)
        self.ws_path = base_path + "/ws"
        self._interval_s = 1.0 / max_hz
        self._src = src
        # Replaced by ``ctx["clock"]``: on a replay the states arrive on a
        # scaled clock, and throttling them against wall time drops the
        # wrong ones (review, minor).
        self._clock: Callable[[], float] = time.monotonic
        self._connections: set[Any] = set()
        self._bus: Bus | None = None
        self._on_estop: Callable[[str], Any] | None = None
        self._on_frame: Callable[[str, float, str, bytes], Any] | None = None
        self._token: str | None = None
        self._state: FaceState | None = None
        self._pending: FaceState | None = None
        self._last_sent_at = 0.0
        self._flusher: asyncio.Task[None] | None = None
        self.sent_count = 0
        self.coalesced_count = 0
        self.dropped_frame_count = 0

    async def start(self, ctx: dict[str, Any]) -> None:
        self._bus = ctx.get("bus")
        self._on_estop = ctx.get("on_estop")
        self._on_frame = ctx.get("on_frame")
        self._token = ctx.get("token")
        self._clock = ctx.get("clock") or time.monotonic
        if self._flusher is None:
            self._flusher = asyncio.create_task(self._flush_loop())

    async def render(self, state: FaceState) -> None:
        self._state = state
        now = self._clock()
        if state.blink or now - self._last_sent_at >= self._interval_s:
            self._pending = None
            self._last_sent_at = now
            await self._broadcast(_face_envelope(state, self._src))
        else:
            if self._pending is not None:
                self.coalesced_count += 1
            self._pending = state

    async def stop(self) -> None:
        if self._flusher is not None:
            self._flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flusher
            self._flusher = None
        for connection in list(self._connections):
            await _safe_close(connection)
        self._connections.clear()

    async def send_metric(self, topic: str, data: dict[str, Any]) -> None:
        """Forward a ``metric.*`` payload to the pages' debug HUD."""
        await self._broadcast(
            Envelope(kind="metric", topic=topic, src=self._src, data=data).to_dict()
        )

    def connection_count(self) -> int:
        return len(self._connections)

    async def attach(self, connection: Any, *, path: str | None = None) -> None:
        """Serve one face page until it disconnects.

        ``path`` is the request path (query included) the page connected
        with; it is required when a token is configured.
        """
        if self._token is not None and _query_token(path) != self._token:
            logger.warning("face page rejected: missing or invalid token")
            await _safe_close(connection, code=1008, reason="invalid token")
            return

        self._connections.add(connection)
        try:
            if self._state is not None:
                await _safe_send(connection, json.dumps(_face_envelope(self._state, self._src)))
            async for message in connection:
                await self._on_message(message)
        finally:
            self._connections.discard(connection)

    async def _on_message(self, message: str | bytes) -> None:
        if isinstance(message, bytes | bytearray):
            await self._on_binary(bytes(message))
            return
        try:
            payload = json.loads(message)
        except ValueError:
            logger.warning("face page sent invalid JSON (%d bytes), ignored", len(message))
            return
        kind = payload.get("type")
        if kind == "tap":
            await self._publish_touch(payload)
        elif kind == "estop":
            await self._fire_estop(payload)
        else:
            logger.warning("face page sent an unknown message type: %r", kind)

    async def _on_binary(self, message: bytes) -> None:
        try:
            frame = decode_frame(message, default_topic=FRAME_BROWSER_TOPIC)
        except ValueError as exc:
            logger.warning("face page sent an undecodable frame: %s", exc)
            return
        if self._on_frame is None:
            self.dropped_frame_count += 1
            return
        result = self._on_frame(frame.topic, frame.ts_ms, JPEG_CONTENT_TYPE, frame.jpeg)
        if inspect.isawaitable(result):
            await result

    async def _publish_touch(self, payload: dict[str, Any]) -> None:
        if self._bus is None:
            logger.warning("face page tap ignored: WebFace started without a bus")
            return
        intensity = float(payload.get("intensity", 1.0))
        await self._bus.publish(
            TOUCHED_TOPIC, {"where": "screen", "intensity": intensity}, kind="percept"
        )

    async def _fire_estop(self, payload: dict[str, Any]) -> None:
        """Ask for an emergency stop, once: on the bus, or through ``on_estop``.

        `TOPICS.SAFETY_ESTOP` is the request every component reads (the core
        subscribes and calls `SafetyGuard.stop_all`). ``on_estop`` is the
        fallback for a `WebFace` served standalone, with no bus.
        """
        reason = str(payload.get("reason", "face_page"))
        if self._bus is not None:
            await self._bus.publish(TOPICS.SAFETY_ESTOP, {"reason": reason}, kind="cmd")
            return
        if self._on_estop is None:
            logger.error(
                "face page E-STOP ignored: WebFace has neither a bus nor an on_estop callback"
            )
            return
        result = self._on_estop(reason)
        if inspect.isawaitable(result):
            await result

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            if self._pending is None:
                continue
            state, self._pending = self._pending, None
            self._last_sent_at = self._clock()
            await self._broadcast(_face_envelope(state, self._src))

    async def _broadcast(self, envelope: dict[str, Any]) -> None:
        if not self._connections:
            return
        text = json.dumps(envelope)
        self.sent_count += 1
        for connection in list(self._connections):
            if not await _safe_send(connection, text):
                self._connections.discard(connection)


def _face_envelope(state: FaceState, src: str) -> dict[str, Any]:
    return Envelope(kind="state", topic=TOPICS.FACE_STATE, src=src, data=state.to_dict()).to_dict()


def _query_token(path: str | None) -> str | None:
    if not path:
        return None
    values = parse_qs(urlsplit(path).query).get("token")
    return values[0] if values else None


async def _safe_send(connection: Any, text: str) -> bool:
    try:
        await connection.send(text)
        return True
    except Exception as exc:  # the page went away mid-broadcast
        logger.info("face page dropped during send: %s", exc)
        return False


async def _safe_close(connection: Any, *, code: int = 1000, reason: str = "") -> None:
    try:
        await connection.close(code, reason)
    except TypeError:
        await connection.close()
    except Exception as exc:
        logger.info("face page close failed: %s", exc)


async def serve_face(
    face: WebFace,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    token: str | None = None,
) -> Any:
    """Serve the face page and its WebSocket standalone, without a hub.

    Used by ``python -m asimoov.faces.web --demo`` and by the tests; on a
    real robot WS1's hub owns the port and mounts `make_process_request`
    itself. Returns the running ``websockets`` server.
    """
    from websockets.asyncio.server import serve
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    hook = make_process_request()

    def process_request(connection: Any, request: Any) -> Any:
        response = hook(request.path)
        if response is None:
            return None
        reason = {200: "OK", 301: "Moved Permanently", 404: "Not Found"}[response.status]
        return Response(response.status, reason, Headers(response.headers), response.body)

    async def handler(connection: Any) -> None:
        path = connection.request.path
        if urlsplit(path).path != FACE_WS_PATH:
            await connection.close(1008, "unknown path")
            return
        await face.attach(connection, path=path)

    await face.start({"token": token})
    return await serve(handler, host, port, process_request=process_request)


DEMO_EMOTION_S = 3.0


def demo_state(elapsed_s: float, *, ts: float | None = None) -> FaceState:
    """The scripted `FaceState` of the standalone demo at ``elapsed_s``.

    A 27 s loop: each of the nine emotions for 3 s, gaze drifting, a blink
    every 4 s, and a talking burst (lip sine) inside each emotion.
    """
    emotion = EMOTIONS[int(elapsed_s // DEMO_EMOTION_S) % len(EMOTIONS)]
    phase = elapsed_s % DEMO_EMOTION_S
    talking = 1.0 < phase < 2.4
    return FaceState(
        emotion=emotion,
        intensity=1.0,
        gaze=FaceGaze(x=0.6 * math.sin(elapsed_s * 0.7), y=0.3 * math.sin(elapsed_s * 0.4)),
        lip=abs(math.sin(elapsed_s * 9.0)) if talking else 0.0,
        blink=elapsed_s % 4.0 < 0.05,
        eyelids=0.0,
        talking=talking,
        ts=ts if ts is not None else time.time(),
    )
