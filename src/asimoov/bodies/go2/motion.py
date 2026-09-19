"""The Go2's single motion writer.

Neon ran one `asyncio` task per movement and one more per turn, all writing
to ``rt/wirelesscontroller`` at the same time, with no way to interrupt a
turn (plan.md section 2, bug 8). Here exactly one `_sender` task exists
between `start` and `stop`; it reads one `JoystickState` at 10 Hz. `move`
and `turn` replace that state instead of racing it, and every commanded
motion carries a deadline.

Frames of reference
-------------------
``vx``/``vy``/``wz`` follow ROS REP-103 like the rest of ASIMOOV: ``vx`` > 0
forward, ``vy`` > 0 to the robot's own left, ``wz`` > 0 counter-clockwise
seen from above, i.e. turning left. The Go2's wireless controller is the
other way round on both lateral axes (``lx`` > 0 strafes right, ``rx`` > 0
yaws right), hence the two sign flips in `joystick_from_velocity`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

DEFAULT_RATE_HZ = 10.0
DEFAULT_WATCHDOG_S = 0.5
ZERO_FRAMES_AFTER_STOP = 3

MOVE_RUNNING = "running"
MOVE_OK = "ok"
MOVE_INTERRUPTED = "interrupted"
MOVE_ERROR = "error"

JoystickSender = Callable[[float, float, float, float], None]


@dataclass(frozen=True)
class JoystickState:
    """One wireless-controller frame, in the Go2's own axes."""

    lx: float = 0.0
    ly: float = 0.0
    rx: float = 0.0
    ry: float = 0.0

    def is_zero(self) -> bool:
        return self.lx == 0.0 and self.ly == 0.0 and self.rx == 0.0 and self.ry == 0.0


ZERO = JoystickState()


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def joystick_from_velocity(
    vx: float, vy: float, wz: float, *, max_speed: float, max_yaw_rate: float
) -> JoystickState:
    """Map normalized REP-103 velocities in [-1, 1] to a joystick frame."""
    return JoystickState(
        lx=-_clamp(vy, 1.0) * max_speed,
        ly=_clamp(vx, 1.0) * max_speed,
        rx=-_clamp(wz, 1.0) * max_yaw_rate,
    )


class MotionController:
    """Single-writer joystick loop with deadlines, watchdog and e-stop.

    Args:
        send: Called with ``(lx, ly, rx, ry)`` for every frame; the adapter
            passes `sport.SportClient.send_joystick`. Must not block.
        stop_move: Optional coroutine function sending ``StopMove``; fired
            and *not* awaited by `stop_all`, which has a 200 ms budget.
        max_speed / max_yaw_rate: `BodyManifest.limits`, applied to every
            commanded velocity.
        max_continuous_motion_s: Upper bound on a timed motion's deadline.
        on_error: Called with a one-line description of a failure the loop
            cannot act on; the adapter passes `Go2Body._record_error`, so it
            lands in `BodyHealth.errors`.
        clock: Injectable monotonic clock, for tests.
    """

    def __init__(
        self,
        send: JoystickSender,
        *,
        stop_move: Callable[[], Awaitable[None]] | None = None,
        max_speed: float = 0.6,
        max_yaw_rate: float = 0.5,
        max_continuous_motion_s: float = 20.0,
        rate_hz: float = DEFAULT_RATE_HZ,
        watchdog_s: float = DEFAULT_WATCHDOG_S,
        on_error: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._send = send
        self._stop_move = stop_move
        self._on_error = on_error
        self._max_speed = max_speed
        self._max_yaw_rate = max_yaw_rate
        self._max_continuous_motion_s = max_continuous_motion_s
        self._period_s = 1.0 / rate_hz
        self._watchdog_s = watchdog_s
        self._clock = clock

        self._commanded: JoystickState | None = None
        self._deadline: float | None = None
        self._commanded_at: float = 0.0
        self._command_id: int = 0
        self._status: str = MOVE_OK
        self._reason: str | None = None
        self._gaze_rx: float = 0.0
        self._gaze_at: float = 0.0
        self._zero_frames: int = 0
        self._task: asyncio.Task[None] | None = None
        self._last_tick: float = 0.0
        self.sent: int = 0

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start the one and only sender task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._last_tick = self._clock()
        self._task = asyncio.create_task(self._sender(), name="go2-motion-sender")

    async def stop(self) -> None:
        """Cancel the sender task after zeroing the joystick. Idempotent."""
        self._clear("motion controller stopped")
        self._send_now(ZERO)
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # -- commands ----------------------------------------------------------

    def move(self, vx: float, vy: float, wz: float, duration_s: float) -> float:
        """Replace the current target. Returns the effective duration.

        ``duration_s <= 0`` means "until told otherwise": the target then
        expires after ``watchdog_s`` unless `keepalive` refreshes it, which
        is what stops the robot when its commander dies.
        """
        now = self._clock()
        self._commanded = joystick_from_velocity(
            vx, vy, wz, max_speed=self._max_speed, max_yaw_rate=self._max_yaw_rate
        )
        self._commanded_at = now
        self._command_id += 1
        self._status = MOVE_RUNNING
        self._reason = None
        if duration_s > 0:
            effective = min(duration_s, self._max_continuous_motion_s)
            self._deadline = now + effective
            return effective
        self._deadline = None
        return 0.0

    def turn(self, wz: float, duration_s: float) -> float:
        """Replace the current target with a pure rotation (``wz`` > 0 = left)."""
        return self.move(0.0, 0.0, wz, duration_s)

    def keepalive(self) -> None:
        """Refresh a continuous target so the watchdog does not zero it."""
        if self._commanded is not None and self._deadline is None:
            self._commanded_at = self._clock()

    def set_gaze_yaw(self, rx: float) -> None:
        """Set the gaze-driven yaw (already scaled), applied only when idle.

        ``rx`` is in the Go2's frame: negative turns left. It expires after
        ``watchdog_s`` like a continuous motion, so a `look_at` stream that
        stops leaves the robot still.
        """
        self._gaze_rx = rx
        self._gaze_at = self._clock()

    def is_moving(self) -> bool:
        """True while a commanded (non-gaze) motion is active."""
        return self._effective_command() is not None

    @property
    def command_id(self) -> int:
        """Id of the last commanded motion, to be passed to `outcome`."""
        return self._command_id

    def outcome(self, command_id: int) -> tuple[str, str | None]:
        """What became of the motion `move` numbered ``command_id``.

        `MOVE_OK` once its deadline passed, `MOVE_INTERRUPTED` if it was
        cancelled or replaced, `MOVE_ERROR` with a reason if a frame could
        not be sent, `MOVE_RUNNING` while it is still under way.
        """
        self._effective_command()
        if command_id != self._command_id:
            return MOVE_INTERRUPTED, "pre-empted by a newer command"
        return self._status, self._reason

    async def stop_all(self) -> None:
        """E-stop: drop the target, send a zero frame, fire ``StopMove``.

        Never awaits the robot: the sport reply can take seconds and
        `Body.stop_all` has 200 ms.
        """
        self._clear("stopped by stop_all")
        self._send_now(ZERO)
        self._zero_frames = ZERO_FRAMES_AFTER_STOP
        if self._stop_move is not None:
            with contextlib.suppress(RuntimeError):
                task = asyncio.get_running_loop().create_task(self._stop_move())
                task.add_done_callback(self._on_stop_move_done)

    # -- internals ---------------------------------------------------------

    def _clear(self, reason: str) -> None:
        if self._status == MOVE_RUNNING:
            self._status = MOVE_INTERRUPTED
            self._reason = reason
        self._commanded = None
        self._deadline = None
        self._gaze_rx = 0.0

    def _fail(self, reason: str) -> None:
        if self._status == MOVE_RUNNING:
            self._status = MOVE_ERROR
            self._reason = reason
        self._clear(reason)
        self._zero_frames = 0
        if self._on_error is not None:
            self._on_error(reason)

    def _on_stop_move_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        log.exception("go2 motion: StopMove failed", exc_info=exc)
        if self._on_error is not None:
            self._on_error(f"StopMove failed: {exc}")

    def _send_now(self, state: JoystickState) -> None:
        self._send(state.lx, state.ly, state.rx, state.ry)
        self.sent += 1

    def _effective_command(self) -> JoystickState | None:
        if self._commanded is None:
            return None
        now = self._clock()
        if self._deadline is not None:
            if now >= self._deadline:
                self._commanded = None
                self._deadline = None
                self._status = MOVE_OK
                self._reason = None
                return None
        elif now - self._commanded_at > self._watchdog_s:
            self._commanded = None
            self._status = MOVE_INTERRUPTED
            self._reason = "no keepalive within the watchdog"
            return None
        return self._commanded

    def target(self) -> JoystickState:
        """The frame that would be sent right now (commanded, else gaze, else zero)."""
        commanded = self._effective_command()
        if commanded is not None:
            return commanded
        if self._gaze_rx and self._clock() - self._gaze_at <= self._watchdog_s:
            return JoystickState(rx=self._gaze_rx)
        self._gaze_rx = 0.0
        return ZERO

    async def _sender(self) -> None:
        while True:
            await asyncio.sleep(self._period_s)
            now = self._clock()
            if now - self._last_tick > self._watchdog_s:
                # The loop stalled (event-loop starvation, suspended process):
                # whatever was commanded is stale, stop rather than resume.
                self._clear("sender loop stalled")
                self._zero_frames = ZERO_FRAMES_AFTER_STOP
            self._last_tick = now

            target = self.target()
            if not target.is_zero():
                self._zero_frames = ZERO_FRAMES_AFTER_STOP
            elif self._zero_frames > 0:
                self._zero_frames -= 1
            else:
                continue
            try:
                self._send_now(target)
            except Exception as exc:  # noqa: BLE001 - surfaced through on_error
                log.exception("go2 motion: joystick frame failed")
                self._fail(f"joystick frame failed: {exc}")
