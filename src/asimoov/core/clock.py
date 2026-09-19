"""The one clock the mind reads, so a replay can run faster than real life.

Everything time-based in the mind (attention hysteresis, initiative
cooldowns, injection coalescing, forgetting a lost track) reads a `Clock`
instead of calling `time.time()` directly. `asimoov replay --speed N`
swaps in a `ScaledClock` and runs the mind's tick N times faster, so a
fixture recorded over five minutes is asserted in seconds with the same
timings the robot would see live.
"""

from __future__ import annotations

import time
from collections.abc import Callable

Clock = Callable[[], float]


def wall_clock() -> float:
    return time.time()


class ScaledClock:
    """Wall time starting at ``origin`` but flowing ``speed`` times faster."""

    def __init__(self, origin: float, speed: float = 1.0) -> None:
        if speed <= 0:
            raise ValueError(f"speed must be positive, got {speed!r}")
        self.origin = origin
        self.speed = speed
        self._started = time.monotonic()

    def __call__(self) -> float:
        return self.origin + (time.monotonic() - self._started) * self.speed

    def reset(self) -> None:
        """Restart the flow of time from ``origin`` (called when a replay starts)."""
        self._started = time.monotonic()

    def skip_to(self, now: float) -> None:
        """Jump straight to ``now``: a replay skipping a long idle gap."""
        self.origin = now
        self._started = time.monotonic()


class ManualClock:
    """A clock tests move by hand."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now
