"""Neck gaze loop: a 5 Hz P-controller on ``neck_yaw`` / ``neck_pitch``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Callable, Mapping

from asimoov.bodies.inmoov.faults import FaultTracker
from asimoov.bodies.inmoov.link import ChannelSpec, Link
from asimoov.contracts.body import GazeTarget

LOG = logging.getLogger(__name__)

RATE_HZ = 5.0
KP = 0.6
DEAD_ZONE_DEG = 3.0
MAX_RATE_DEG_S = 40.0
MOVE_MS = 200
IDLE_AFTER_S = 8.0
IDLE_AMPLITUDE_DEG = 20.0
IDLE_PERIOD_S = 24.0


class GazeLoop:
    """Drives the neck toward the latest `GazeTarget`, and scans when idle.

    `look_at` is fire-and-forget and may be called at 5 Hz: it only stores
    the target here. One long-lived task does the smoothing, so no caller
    ever spawns a task per target.

    Sign convention (frozen, see ``docs/contracts.md``): ``target.az > 0``
    means the robot's own **left**, and ``neck_yaw`` grows toward the left,
    so the commanded angle is ``rest + az``. A neck wired the other way is
    fixed with ``inverted`` in the firmware's `config.h`, never by flipping
    the sign here.
    """

    def __init__(
        self,
        link: Link,
        channels: Mapping[str, ChannelSpec],
        *,
        clock: Callable[[], float] = time.monotonic,
        faults: FaultTracker | None = None,
    ) -> None:
        self._link = link
        self._faults = faults if faults is not None else FaultTracker()
        self._yaw = channels.get("neck_yaw")
        self._pitch = channels.get("neck_pitch")
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._target_az = 0.0
        self._target_el = 0.0
        self._target_at: float | None = None
        self._yaw_cmd = float(self._yaw.rest_deg) if self._yaw else 0.0
        self._pitch_cmd = float(self._pitch.rest_deg) if self._pitch else 0.0
        self._started_at = clock()

    @property
    def available(self) -> bool:
        return self._yaw is not None

    def set_target(self, target: GazeTarget) -> None:
        self._target_az = target.az
        self._target_el = target.el
        self._target_at = self._clock()

    async def start(self) -> None:
        if not self.available or (self._task is not None and not self._task.done()):
            return
        self._task = asyncio.create_task(self._run(), name="inmoov-gaze")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(1.0 / RATE_HZ)
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the loop outlives one bad tick
                LOG.exception("inmoov gaze: tick failed")
                self._faults.record("gaze", f"tick failed: {exc}")

    async def step(self) -> str | None:
        """One control tick. Returns the command sent, or None if none was."""
        if self._yaw is None:
            return None
        dt_s = 1.0 / RATE_HZ
        desired_az, desired_el = self._desired()

        yaw = self._advance(self._yaw_cmd, self._yaw.clamp(self._yaw.rest_deg + desired_az), dt_s)
        parts = []
        if round(yaw) != round(self._yaw_cmd):
            parts.append(f"{self._yaw.id}:{self._yaw.clamp(yaw)}")
        self._yaw_cmd = yaw

        if self._pitch is not None:
            pitch = self._advance(
                self._pitch_cmd, self._pitch.clamp(self._pitch.rest_deg + desired_el), dt_s
            )
            if round(pitch) != round(self._pitch_cmd):
                parts.append(f"{self._pitch.id}:{self._pitch.clamp(pitch)}")
            self._pitch_cmd = pitch

        if not parts:
            return None
        command = f"M {','.join(parts)} T{MOVE_MS}"
        reply = await self._link.send(command)
        self._faults.record("gaze", None if reply.ok else reply.error or reply.status)
        return command

    def _desired(self) -> tuple[float, float]:
        now = self._clock()
        if self._target_at is None or now - self._target_at > IDLE_AFTER_S:
            phase = 2 * math.pi * (now - self._started_at) / IDLE_PERIOD_S
            return IDLE_AMPLITUDE_DEG * math.sin(phase), 0.0
        return self._target_az, self._target_el

    @staticmethod
    def _advance(current: float, desired: float, dt_s: float) -> float:
        error = desired - current
        if abs(error) < DEAD_ZONE_DEG:
            return current
        step = KP * error
        limit = MAX_RATE_DEG_S * dt_s
        return current + min(max(step, -limit), limit)
