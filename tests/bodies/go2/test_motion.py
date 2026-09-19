"""`MotionController`: one writer, deadlines, watchdog, e-stop."""

from __future__ import annotations

import asyncio
import time

from asimoov.bodies.go2.motion import (
    MOVE_ERROR,
    MOVE_INTERRUPTED,
    MOVE_OK,
    MOVE_RUNNING,
    MotionController,
    joystick_from_velocity,
)


class Recorder:
    def __init__(self) -> None:
        self.frames: list[tuple[float, float, float, float]] = []

    def __call__(self, lx: float, ly: float, rx: float, ry: float) -> None:
        self.frames.append((lx, ly, rx, ry))

    @property
    def last(self) -> tuple[float, float, float, float]:
        return self.frames[-1]


def make(**kwargs: object) -> tuple[MotionController, Recorder]:
    recorder = Recorder()
    controller = MotionController(recorder, **kwargs)  # type: ignore[arg-type]
    return controller, recorder


def test_velocity_mapping_signs() -> None:
    forward = joystick_from_velocity(1.0, 0.0, 0.0, max_speed=0.6, max_yaw_rate=0.5)
    assert forward.ly > 0 and forward.lx == 0 and forward.rx == 0

    # vy > 0 is the robot's left; the Go2 joystick's lx is positive to the right.
    left = joystick_from_velocity(0.0, 1.0, 0.0, max_speed=0.6, max_yaw_rate=0.5)
    assert left.lx < 0

    # wz > 0 is counter-clockwise (left); the Go2 yaws left for rx < 0.
    turn_left = joystick_from_velocity(0.0, 0.0, 1.0, max_speed=0.6, max_yaw_rate=0.5)
    assert turn_left.rx < 0


def test_velocity_is_clamped_by_limits() -> None:
    state = joystick_from_velocity(5.0, 0.0, -5.0, max_speed=0.6, max_yaw_rate=0.5)
    assert state.ly == 0.6
    assert state.rx == 0.5


async def test_single_sender_task() -> None:
    controller, recorder = make()
    await controller.start()
    await controller.start()  # idempotent
    controller.move(1.0, 0.0, 0.0, 1.0)
    await asyncio.sleep(0.25)

    senders = [t for t in asyncio.all_tasks() if t.get_name() == "go2-motion-sender"]
    assert len(senders) == 1
    assert len(recorder.frames) >= 2
    assert all(frame[1] > 0 for frame in recorder.frames)
    await controller.stop()


async def test_move_replaces_the_target_instead_of_racing_it() -> None:
    controller, recorder = make()
    await controller.start()
    controller.move(1.0, 0.0, 0.0, 1.0)
    await asyncio.sleep(0.15)
    controller.move(-1.0, 0.0, 0.0, 1.0)
    await asyncio.sleep(0.15)
    assert recorder.last[1] < 0
    await controller.stop()


async def test_deadline_stops_the_motion() -> None:
    controller, recorder = make()
    await controller.start()
    controller.move(1.0, 0.0, 0.0, 0.2)
    await asyncio.sleep(0.45)
    assert controller.target().is_zero()
    assert recorder.last == (0.0, 0.0, 0.0, 0.0)
    await controller.stop()


async def test_watchdog_zeroes_a_continuous_motion() -> None:
    controller, recorder = make(watchdog_s=0.3)
    await controller.start()
    controller.move(1.0, 0.0, 0.0, 0.0)  # continuous
    await asyncio.sleep(0.2)
    assert not controller.target().is_zero()
    await asyncio.sleep(0.4)
    assert controller.target().is_zero()
    assert recorder.last == (0.0, 0.0, 0.0, 0.0)
    await controller.stop()


async def test_keepalive_extends_a_continuous_motion() -> None:
    controller, _ = make(watchdog_s=0.3)
    await controller.start()
    controller.move(1.0, 0.0, 0.0, 0.0)
    for _ in range(4):
        await asyncio.sleep(0.1)
        controller.keepalive()
    assert not controller.target().is_zero()
    await controller.stop()


async def test_stop_all_is_immediate_and_sends_stop_move() -> None:
    stopped = asyncio.Event()

    async def stop_move() -> None:
        await asyncio.sleep(1.0)  # a real sport reply is slow
        stopped.set()

    controller, recorder = make(stop_move=stop_move)
    await controller.start()
    controller.move(1.0, 0.0, 0.0, 5.0)
    await asyncio.sleep(0.15)

    started = time.monotonic()
    await controller.stop_all()
    elapsed = time.monotonic() - started

    assert elapsed < 0.2, f"stop_all took {elapsed * 1000:.0f} ms"
    assert recorder.last == (0.0, 0.0, 0.0, 0.0)
    assert controller.target().is_zero()
    await asyncio.sleep(0.15)
    assert recorder.last == (0.0, 0.0, 0.0, 0.0), "must not resume after stop_all"
    await controller.stop()


async def test_gaze_yaw_only_applies_when_no_motion_is_commanded() -> None:
    controller, _ = make()
    await controller.start()
    controller.set_gaze_yaw(-0.2)
    assert controller.target().rx == -0.2

    controller.move(1.0, 0.0, 0.0, 1.0)
    assert controller.is_moving()
    assert controller.target().rx == 0.0
    await controller.stop()


async def test_gaze_yaw_expires_with_the_watchdog() -> None:
    controller, _ = make(watchdog_s=0.2)
    await controller.start()
    controller.set_gaze_yaw(-0.2)
    await asyncio.sleep(0.35)
    assert controller.target().is_zero()
    await controller.stop()


# -- outcomes ---------------------------------------------------------------


async def test_outcome_is_ok_once_the_deadline_passed() -> None:
    controller, _ = make()
    await controller.start()
    controller.move(1.0, 0.0, 0.0, 0.15)
    command = controller.command_id
    assert controller.outcome(command) == (MOVE_RUNNING, None)

    await asyncio.sleep(0.3)
    assert controller.outcome(command) == (MOVE_OK, None)
    await controller.stop()


async def test_outcome_is_interrupted_after_stop_all() -> None:
    controller, _ = make()
    await controller.start()
    controller.move(1.0, 0.0, 0.0, 5.0)
    command = controller.command_id

    await controller.stop_all()
    status, reason = controller.outcome(command)
    assert status == MOVE_INTERRUPTED
    assert reason
    await controller.stop()


async def test_outcome_is_interrupted_when_a_newer_move_pre_empts_it() -> None:
    controller, _ = make()
    await controller.start()
    controller.move(1.0, 0.0, 0.0, 5.0)
    first = controller.command_id
    controller.move(-1.0, 0.0, 0.0, 5.0)

    assert controller.outcome(first)[0] == MOVE_INTERRUPTED
    assert controller.outcome(controller.command_id)[0] == MOVE_RUNNING
    await controller.stop()


async def test_a_frame_the_link_refuses_is_an_error_not_a_silence() -> None:
    errors: list[str] = []

    def send(lx: float, ly: float, rx: float, ry: float) -> None:
        if (lx, ly, rx, ry) != (0.0, 0.0, 0.0, 0.0):
            raise RuntimeError("data channel closed")

    controller = MotionController(send, on_error=errors.append)
    await controller.start()
    controller.move(1.0, 0.0, 0.0, 5.0)
    command = controller.command_id
    await asyncio.sleep(0.25)

    status, reason = controller.outcome(command)
    assert status == MOVE_ERROR
    assert "data channel closed" in (reason or "")
    assert [error for error in errors if "data channel closed" in error]
    assert controller.running, "one failed frame must not kill the single writer"
    await controller.stop()


async def test_a_failing_stop_move_is_reported_not_swallowed() -> None:
    errors: list[str] = []

    async def stop_move() -> None:
        raise RuntimeError("StopMove refused")

    controller, _ = make(stop_move=stop_move, on_error=errors.append)
    await controller.start()
    controller.move(1.0, 0.0, 0.0, 5.0)
    await controller.stop_all()
    await asyncio.sleep(0.05)

    assert [error for error in errors if "StopMove refused" in error]
    await controller.stop()
