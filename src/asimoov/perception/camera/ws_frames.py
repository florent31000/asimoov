"""Frames pushed to the hub by another module (``frame.browser``, ``frame.go2``).

The phone or the browser captures, the hub relays binary ``frame.*`` messages,
and this source decodes the JPEG locally. The decoded pixels stay in this
process: nothing is re-published and nothing is written to disk.
"""

from __future__ import annotations

import asyncio
import logging
import time

from asimoov.core.bus.local import topic_matches
from asimoov.perception import PerceptionError
from asimoov.perception.camera.base import DEFAULT_FPS, CameraSource, Frame
from asimoov.perception.frames import decode_frame

log = logging.getLogger(__name__)

BROWSER_TOPIC = "frame.browser"
GO2_TOPIC = "frame.go2"
ANY_FRAME_TOPIC = "frame.*"


class WsFrameCamera(CameraSource):
    """Latest-frame-wins source fed by the hub's binary frame messages.

    Only the most recent frame is kept: a slow pipeline must skip frames, not
    process a growing backlog of stale ones.
    """

    def __init__(
        self,
        hub,
        *,
        topic: str = ANY_FRAME_TOPIC,
        fps: float = DEFAULT_FPS,
    ) -> None:
        self._hub = hub
        self.topic = topic
        self.fps = fps
        self._pending: bytes | None = None
        self._frames = None
        self._arrived = asyncio.Event()
        self._seq = 0
        self._next_at = 0.0
        self._running = False
        self.dropped = 0

    async def start(self) -> None:
        try:
            import cv2  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on the [vision] extra
            raise PerceptionError("ws_frames needs `pip install asimoov[vision]`") from exc
        self._frames = self._hub.subscribe_frames(self.on_binary)
        self._running = True
        self._next_at = time.monotonic()

    async def stop(self) -> None:
        self._running = False
        if self._frames is not None:
            self._frames.unsubscribe()
            self._frames = None
        self._arrived.set()

    async def on_binary(self, message: bytes) -> None:
        """Hub callback: keep the newest JPEG matching ``topic``."""
        try:
            frame = decode_frame(message, default_topic=self._default_topic())
        except ValueError as exc:
            log.warning("dropping undecodable frame message: %s", exc)
            return
        if not topic_matches(self.topic, frame.topic):
            return
        if self._pending is not None:
            self.dropped += 1
        self._pending = frame.jpeg
        self._arrived.set()

    def _default_topic(self) -> str | None:
        return None if self.topic.endswith("*") else self.topic

    async def read(self) -> Frame | None:
        import cv2
        import numpy as np

        while self._running:
            wait = self._next_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            if self._pending is None:
                self._arrived.clear()
                await self._arrived.wait()
                continue
            jpeg, self._pending = self._pending, None
            self._arrived.clear()
            self._next_at = max(self._next_at + 1.0 / self.fps, time.monotonic())
            image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                log.warning("dropping frame: JPEG did not decode")
                continue
            self._seq += 1
            return Frame(bgr=image, ts=time.time(), seq=self._seq)
        return None
