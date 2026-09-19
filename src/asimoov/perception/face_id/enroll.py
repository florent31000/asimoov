"""Enrolment state machine: 5 good embeddings of one track within 10 seconds.

Driven by the ``perception.face_id.enroll`` command. It only accumulates and
decides; writing to the `MemoryStore` and replying is the module's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_SAMPLES = 5
DEFAULT_MIN_QUALITY = 0.6
DEFAULT_TIMEOUT_S = 10.0

REASON_TIMEOUT = "timeout"
REASON_TRACK_LOST = "track_lost"
REASON_CANCELLED = "cancelled"


@dataclass(frozen=True)
class EnrollmentResult:
    """Outcome of one enrolment, mapped straight onto the `reply` payload."""

    ok: bool
    samples: int
    person_id: str
    track_id: str
    reason: str | None = None
    best_quality: float = 0.0

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "samples": self.samples,
            "person_id": self.person_id,
            "track_id": self.track_id,
        }
        if not self.ok:
            payload["reason"] = self.reason
            payload["best_quality"] = round(self.best_quality, 3)
        return payload


@dataclass
class Enrollment:
    """Collect ``samples`` embeddings above ``min_quality`` for one track."""

    track_id: str
    person_id: str
    started_at: float
    name: str | None = None
    samples: int = DEFAULT_SAMPLES
    min_quality: float = DEFAULT_MIN_QUALITY
    timeout_s: float = DEFAULT_TIMEOUT_S
    collected: list[tuple[Any, float]] = field(default_factory=list)
    best_quality: float = 0.0
    finished: EnrollmentResult | None = None

    @property
    def done(self) -> bool:
        return self.finished is not None

    def offer(self, track_id: str, vec: Any, quality: float, now: float) -> bool:
        """Offer one embedding; returns True if it was kept.

        Reaching ``samples`` finishes the enrolment successfully.
        """
        if self.done or track_id != self.track_id:
            return False
        self.best_quality = max(self.best_quality, quality)
        if quality <= self.min_quality:
            return False
        self.collected.append((vec, quality))
        if len(self.collected) >= self.samples:
            self.finished = EnrollmentResult(
                ok=True,
                samples=len(self.collected),
                person_id=self.person_id,
                track_id=self.track_id,
                best_quality=self.best_quality,
            )
        return True

    def tick(self, now: float) -> EnrollmentResult | None:
        """Expire the enrolment if the window elapsed. Returns the result."""
        if self.done:
            return self.finished
        if now - self.started_at >= self.timeout_s:
            self.finished = self._failure(REASON_TIMEOUT)
        return self.finished

    def track_lost(self) -> EnrollmentResult | None:
        """The tracked face disappeared before enough samples were collected."""
        if not self.done:
            self.finished = self._failure(REASON_TRACK_LOST)
        return self.finished

    def cancel(self) -> EnrollmentResult | None:
        """The process is shutting down or a new enrolment superseded this one."""
        if not self.done:
            self.finished = self._failure(REASON_CANCELLED)
        return self.finished

    def _failure(self, reason: str) -> EnrollmentResult:
        return EnrollmentResult(
            ok=False,
            samples=len(self.collected),
            person_id=self.person_id,
            track_id=self.track_id,
            reason=reason,
            best_quality=self.best_quality,
        )
