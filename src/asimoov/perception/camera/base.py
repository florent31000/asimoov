"""Camera source contract: an async iterator of BGR frames, capped at N fps.

A ``Frame`` never leaves the perception process: it is decoded, measured, and
dropped. Nothing here writes an image to disk.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

DEFAULT_FPS = 5.0


@dataclass(frozen=True)
class Frame:
    """One captured image.

    Attributes:
        bgr: HxWx3 ``uint8`` numpy array, OpenCV channel order.
        ts: Unix seconds at capture time.
        seq: Monotonic counter, so a consumer can detect drops.
    """

    bgr: Any
    ts: float
    seq: int

    @property
    def width(self) -> int:
        return int(self.bgr.shape[1])

    @property
    def height(self) -> int:
        return int(self.bgr.shape[0])


class CameraSource(ABC):
    """A source of frames, started and stopped by the owning module."""

    @abstractmethod
    async def start(self) -> None:
        """Open the device or subscribe to the frame topic (< 100 ms)."""

    @abstractmethod
    async def stop(self) -> None:
        """Release the device or drop the subscription. Idempotent."""

    @abstractmethod
    async def read(self) -> Frame | None:
        """Return the next frame, or None when the source is exhausted.

        Blocks until a frame is available; respects the configured fps.
        """

    def __aiter__(self) -> CameraSource:
        return self

    async def __anext__(self) -> Frame:
        frame = await self.read()
        if frame is None:
            raise StopAsyncIteration
        return frame
