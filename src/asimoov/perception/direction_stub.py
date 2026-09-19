"""Sound direction of arrival: the interface, and the V1 implementation that
has none.

V1 attributes speech to the only visible person, or to the person in
attention (core side). A ReSpeaker-style DOA array replaces
`NullDirectionEstimator` without touching any caller: `az` keeps the frozen
sign convention, positive = the robot's own left.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from asimoov.contracts.percepts import Bearing


@runtime_checkable
class DirectionEstimator(Protocol):
    """Where the current sound is coming from, if the hardware can tell."""

    def estimate(self) -> Bearing | None:
        """Latest direction of arrival, or None when unavailable.

        Non-blocking: called on every speech transition.
        """
        ...


class NullDirectionEstimator:
    """No microphone array: every estimate is None."""

    def estimate(self) -> Bearing | None:
        return None
