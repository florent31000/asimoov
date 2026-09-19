"""Session thresholds: when to renew, truncate or drop a voice session.

Neon never truncated or renewed its Realtime session, so latency grew over
the minutes (plan.md section 2, item 2). The policy is pure and lives here
so the core tests it and WS2's `SessionManager` applies it rather than
re-deciding the numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

RENEW_TOKENS = 24_000
RENEW_AGE_S = 25 * 60
IDLE_DISCONNECT_S = 10 * 60
MAX_ITEMS = 40


@dataclass(frozen=True)
class SessionThresholds:
    """The four numbers from plan.md section 4.4."""

    max_total_tokens: int = RENEW_TOKENS
    max_age_s: float = RENEW_AGE_S
    idle_disconnect_s: float = IDLE_DISCONNECT_S
    max_items: int = MAX_ITEMS


@dataclass
class SessionState:
    """What the provider tells us about the live session."""

    started_at: float
    last_activity_at: float
    total_tokens: int = 0
    items: int = 0
    thresholds: SessionThresholds = field(default_factory=SessionThresholds)

    def touch(self, now: float, *, total_tokens: int | None = None, items: int | None = None) -> None:
        self.last_activity_at = now
        if total_tokens is not None:
            self.total_tokens = total_tokens
        if items is not None:
            self.items = items

    def renew_reason(self, now: float) -> str | None:
        """Why the session should be renewed now, or None."""
        if self.total_tokens > self.thresholds.max_total_tokens:
            return "tokens"
        if now - self.started_at > self.thresholds.max_age_s:
            return "age"
        return None

    def should_disconnect(self, now: float) -> bool:
        """True once the session has been idle long enough to drop the link."""
        return now - self.last_activity_at > self.thresholds.idle_disconnect_s

    def items_to_delete(self) -> int:
        """How many oldest conversation items to delete (light truncation)."""
        return max(0, self.items - self.thresholds.max_items)
