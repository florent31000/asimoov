"""`ServoFace`: the InMoov face, i.e. the LED matrix and the eyelids."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from asimoov.bodies.inmoov.faults import FaultTracker
from asimoov.bodies.inmoov.link import ChannelSpec, Link
from asimoov.contracts.face import FaceRenderer, FaceState

EYELIDS_THRESHOLD = 0.05


class ServoFace(FaceRenderer):
    """Emotion to the 12x8 LED matrix (``F``), eyelids to their servo.

    The mouth is not rendered here: ``lip`` drives the jaw servo through
    `jaw.JawDriver`, at its own rate.
    """

    def __init__(
        self,
        link: Link,
        channels: Mapping[str, ChannelSpec],
        *,
        faults: FaultTracker | None = None,
    ) -> None:
        self._link = link
        self._faults = faults if faults is not None else FaultTracker()
        self._eyelids = channels.get("eyelids")
        self._emotion: str | None = None
        self._eyelids_value: float | None = None

    async def start(self, ctx: dict[str, Any]) -> None:
        self._emotion = None
        self._eyelids_value = None

    async def render(self, state: FaceState) -> None:
        if state.emotion != self._emotion:
            reply = await self._link.send(f"F {state.emotion}")
            if reply.ok:
                self._emotion = state.emotion
                self._faults.record("emotion", None)
            else:
                self._faults.record("emotion", reply.error or reply.status or "no reply")

        if self._eyelids is None:
            return
        eyelids = min(max(state.eyelids, 0.0), 1.0)
        if self._eyelids_value is not None and abs(eyelids - self._eyelids_value) < EYELIDS_THRESHOLD:
            return
        # eyelids 0 = wide open = the channel's min_deg, 1 = closed = max_deg.
        span = self._eyelids.max_deg - self._eyelids.min_deg
        degrees = self._eyelids.clamp(self._eyelids.min_deg + span * eyelids)
        reply = await self._link.send(f"S {self._eyelids.id} {degrees}")
        if reply.ok:
            self._eyelids_value = eyelids
            self._faults.record("eyelids", None)
        else:
            self._faults.record("eyelids", reply.error or reply.status or "no reply")

    async def stop(self) -> None:
        self._emotion = None
        self._eyelids_value = None
