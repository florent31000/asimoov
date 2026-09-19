"""Local camera via OpenCV (``opencv:0``), read in a worker thread."""

from __future__ import annotations

import asyncio
import time

from asimoov.perception import PerceptionError
from asimoov.perception.camera.base import DEFAULT_FPS, CameraSource, Frame


class OpenCVCamera(CameraSource):
    """``cv2.VideoCapture`` throttled to ``fps``.

    ``VideoCapture.read`` blocks, so it runs in the default executor and the
    asyncio loop stays free for the hub connection.
    """

    def __init__(
        self,
        index: int = 0,
        *,
        fps: float = DEFAULT_FPS,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        self.index = index
        self.fps = fps
        self.width = width
        self.height = height
        self._capture = None
        self._seq = 0
        self._next_at = 0.0

    async def start(self) -> None:
        try:
            import cv2
        except ImportError as exc:  # pragma: no cover - depends on the [vision] extra
            raise PerceptionError("opencv camera needs `pip install asimoov[vision]`") from exc
        capture = await asyncio.to_thread(cv2.VideoCapture, self.index)
        if not capture.isOpened():
            capture.release()
            raise PerceptionError(f"cannot open camera index {self.index}")
        if self.width:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        if self.height:
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self._capture = capture
        self._next_at = time.monotonic()

    async def stop(self) -> None:
        capture, self._capture = self._capture, None
        if capture is not None:
            await asyncio.to_thread(capture.release)

    async def read(self) -> Frame | None:
        capture = self._capture
        if capture is None:
            return None
        wait = self._next_at - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._next_at = max(self._next_at + 1.0 / self.fps, time.monotonic())
        ok, image = await asyncio.to_thread(capture.read)
        if not ok or image is None:
            return None
        self._seq += 1
        return Frame(bgr=image, ts=time.time(), seq=self._seq)
