"""Consecutive-failure counters behind `BodyHealth.errors`."""

from __future__ import annotations

ERROR_THRESHOLD = 3


class FaultTracker:
    """Surfaces a source once it has failed ``threshold`` times in a row.

    The gaze, jaw and face loops run at 5-25 Hz against a link that drops a
    reply now and then; a single refusal is noise, a sustained one is a
    fault the operator must see.
    """

    def __init__(self, threshold: int = ERROR_THRESHOLD) -> None:
        self._threshold = threshold
        self._counts: dict[str, int] = {}
        self._messages: dict[str, str] = {}

    def record(self, source: str, error: str | None) -> None:
        """Count one failure of ``source``, or clear it when ``error`` is None."""
        if error is None:
            self._counts.pop(source, None)
            self._messages.pop(source, None)
            return
        self._counts[source] = self._counts.get(source, 0) + 1
        self._messages[source] = error

    def reset(self) -> None:
        self._counts.clear()
        self._messages.clear()

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(
            f"{source}: {self._messages[source]} (x{count})"
            for source, count in sorted(self._counts.items())
            if count >= self._threshold
        )
