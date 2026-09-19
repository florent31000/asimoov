"""`Injector`: perception updates fed to the LLM without drowning it.

Policy from plan.md section 4.4: coalesce for one second, never inject
while a response is streaming (queue and flush at ``response.done``), at
most six injections per minute, drop a duplicate seen in the last minute,
300 characters maximum.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from asimoov.contracts.vocab import TOPICS
from asimoov.core.clock import Clock, wall_clock

log = logging.getLogger(__name__)

COALESCE_S = 1.0
BUDGET_PER_MIN = 6
DEDUP_S = 60.0
MAX_CHARS = 300

DeliverHook = Callable[[str], Awaitable[None]]


class Injector:
    """Queues, coalesces and rate-limits system-role text sent to the provider."""

    def __init__(
        self,
        deliver: DeliverHook,
        *,
        bus=None,
        clock: Clock = wall_clock,
        coalesce_s: float = COALESCE_S,
        budget_per_min: int = BUDGET_PER_MIN,
        dedup_s: float = DEDUP_S,
        max_chars: int = MAX_CHARS,
    ) -> None:
        self.deliver = deliver
        self.bus = bus
        self.clock = clock
        self.coalesce_s = coalesce_s
        self.budget_per_min = budget_per_min
        self.dedup_s = dedup_s
        self.max_chars = max_chars
        self.response_active = False
        self.delivered: list[str] = []
        self.dropped_duplicates = 0
        self._queue: list[str] = []
        self._queued_at: float | None = None
        self._recent: dict[str, float] = {}
        self._sent_at: list[float] = []

    @property
    def pending(self) -> tuple[str, ...]:
        return tuple(self._queue)

    def set_response_active(self, active: bool) -> None:
        """Track whether a response is streaming; flushing waits for False."""
        self.response_active = active

    def submit(self, text: str, *, now: float | None = None) -> bool:
        """Queue ``text``. Returns False if it was dropped as a duplicate."""
        text = (text or "").strip()
        if not text:
            return False
        now = self.clock() if now is None else now
        last = self._recent.get(text)
        if last is not None and now - last < self.dedup_s:
            self.dropped_duplicates += 1
            return False
        self._recent[text] = now
        self._recent = {
            key: at for key, at in self._recent.items() if now - at < self.dedup_s
        }
        self._queue.append(text)
        if self._queued_at is None:
            self._queued_at = now
        return True

    def ready(self, now: float | None = None) -> bool:
        """True if something is queued, coalesced, in budget, and no response is active."""
        if not self._queue or self.response_active:
            return False
        now = self.clock() if now is None else now
        if self._queued_at is not None and now - self._queued_at < self.coalesce_s:
            return False
        return self._within_budget(now)

    async def pump(self, now: float | None = None) -> str | None:
        """Deliver the coalesced queue if the policy allows. Returns what was sent."""
        now = self.clock() if now is None else now
        if not self.ready(now):
            return None
        text = " ".join(self._queue)[: self.max_chars]
        self._queue.clear()
        self._queued_at = None
        self._sent_at.append(now)
        self.delivered.append(text)
        await self.deliver(text)
        if self.bus is not None:
            await self.bus.publish(TOPICS.MIND_INJECTION, {"text": text}, kind="cmd")
        return text

    def _within_budget(self, now: float) -> bool:
        self._sent_at = [at for at in self._sent_at if now - at < 60.0]
        if len(self._sent_at) < self.budget_per_min:
            return True
        log.info("injection budget reached (%d/min), holding the queue", self.budget_per_min)
        return False
