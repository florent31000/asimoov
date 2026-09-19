"""Lip sync: `FaceState.lip` to the firmware's ``J`` command."""

from __future__ import annotations

import time
from collections.abc import Callable

from asimoov.bodies.inmoov.faults import FaultTracker
from asimoov.bodies.inmoov.link import Link

LIP_THRESHOLD = 0.05
MAX_HZ = 25.0


class JawDriver:
    """Sends ``J <0-100>`` when the mouth opening really changed.

    `set_face` can be called at 25 Hz; the jaw servo cannot follow that and
    the link would spend its bandwidth on noise, so a new command is only
    sent when ``lip`` moved by at least 5 % and at most 25 times a second.
    """

    def __init__(
        self,
        link: Link,
        *,
        threshold: float = LIP_THRESHOLD,
        max_hz: float = MAX_HZ,
        clock: Callable[[], float] = time.monotonic,
        faults: FaultTracker | None = None,
    ) -> None:
        self._link = link
        self._faults = faults if faults is not None else FaultTracker()
        self._threshold = threshold
        self._min_interval_s = 1.0 / max_hz
        self._clock = clock
        self._last_lip: float | None = None
        self._last_sent_at = 0.0

    async def update(self, lip: float) -> bool:
        """Push ``lip`` (in [0, 1]) to the jaw. Returns True if a command went out."""
        lip = min(max(lip, 0.0), 1.0)
        now = self._clock()
        if self._last_lip is not None:
            if abs(lip - self._last_lip) < self._threshold:
                return False
            if now - self._last_sent_at < self._min_interval_s:
                return False
        reply = await self._link.send(f"J {int(round(lip * 100))}")
        if not reply.ok:
            self._faults.record("jaw", reply.error or reply.status or "no reply")
            return False
        self._faults.record("jaw", None)
        self._last_lip = lip
        self._last_sent_at = now
        return True
