"""Front camera of the Go2 as binary ``frame.go2`` bus messages.

Optional: only started when ``robot.yaml`` sets ``body.config.frames.enabled``
and the body has a bus that carries frames. The aiortc video track delivers
~30 decoded fps; only `fps` of them are encoded to JPEG and published, because
the consumer (perception) runs far slower than the link.

Frames use the frozen codec of `contracts.frames`, like the face page: the
hub relays those bytes untouched, so perception decodes them with the same
function whatever the camera is.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from asimoov.contracts.frames import TOPIC_FRAME_GO2, encode_frame

log = logging.getLogger(__name__)

DEFAULT_FPS = 5.0

SendFrameFn = Callable[[bytes], Awaitable[None]]
EncodeFn = Callable[[Any], bytes]


class Go2FrameSource:
    """Subscribe to the robot's video track and publish JPEG frames.

    Args:
        conn: The vendored `UnitreeWebRTCConnection`.
        send_frame: ``async send_frame(bytes)``, the bus frame channel.
        fps: Publish rate; frames arriving in between are dropped.
        encode: Frame -> JPEG bytes. Defaults to a PyAV mjpeg encoder
            (no Pillow); tests inject their own.
        clock: Injectable monotonic clock, for tests.
    """

    def __init__(
        self,
        conn: Any,
        *,
        send_frame: SendFrameFn,
        fps: float = DEFAULT_FPS,
        encode: EncodeFn | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._conn = conn
        self._send_frame = send_frame
        self._min_period_s = 1.0 / fps if fps > 0 else 0.0
        self._encode = encode or _PyAvJpegEncoder()
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._last_sent = float("-inf")
        self._seq = 0
        self.published = 0

    async def start(self) -> None:
        self._conn.video.add_track_callback(self._on_track)
        self._conn.datachannel.switchVideoChannel(True)

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        with contextlib.suppress(Exception):
            self._conn.datachannel.switchVideoChannel(False)

    async def _on_track(self, track: Any) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._read(track), name="go2-frames")
        self._task.add_done_callback(_log_failure)

    async def _read(self, track: Any) -> None:
        while True:
            frame = await track.recv()
            now = self._clock()
            if now - self._last_sent < self._min_period_s:
                continue
            self._last_sent = now
            try:
                jpeg = self._encode(frame)
            except Exception as exc:  # noqa: BLE001 - one bad frame must not kill the stream
                log.warning("go2 frame encoding failed: %s", exc)
                continue
            self._seq += 1
            await self._send_frame(
                encode_frame(
                    TOPIC_FRAME_GO2, jpeg, seq=self._seq, ts_ms=int(time.time() * 1000)
                )
            )
            self.published += 1


def _log_failure(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.error("go2 frame reader stopped: %r", error)


class _PyAvJpegEncoder:
    """Encode aiortc `VideoFrame`s to JPEG with PyAV's mjpeg encoder.

    Deliberately not ``frame.to_image()``: that route needs Pillow, which the
    ``[go2]`` extra does not install.
    """

    def __init__(self, quality: int = 80) -> None:
        self._quality = quality
        self._codec: Any = None
        self._size: tuple[int, int] | None = None

    def __call__(self, frame: Any) -> bytes:
        import av

        size = (frame.width, frame.height)
        if self._codec is None or self._size != size:
            codec = av.CodecContext.create("mjpeg", "w")
            codec.width, codec.height = size
            codec.pix_fmt = "yuvj420p"
            codec.options = {"q:v": str(max(2, 31 - self._quality * 29 // 100))}
            self._codec = codec
            self._size = size
        packets = self._codec.encode(frame.reformat(format="yuvj420p"))
        return b"".join(bytes(packet) for packet in packets)
