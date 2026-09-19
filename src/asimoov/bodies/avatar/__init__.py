"""`AvatarBody`: the virtual body behind ``asimoov run robots/avatar``.

A face on a screen and nothing else: no locomotion, no camera. Its gestures
are `FaceState` animations (``nod``, ``shake_head``, ``express``), its
``look_at`` moves the gaze, and its audio comes from the host defaults --
`audio_source` / `audio_sink` return None on purpose, which tells the core
to use the devices from ``robot.yaml: audio`` while the manifest still
declares ``audio.in`` / ``audio.out`` so audio behaviors resolve.

The mind owns ``face.state``. While an animation or a gaze move is running,
the avatar publishes its contribution -- the gaze it is actually showing --
on ``face.overlay`` (``src="bodies.avatar"``), which the mind mixes on top
of its own face for a fraction of a second. It stays silent otherwise.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import deque
from pathlib import Path
from typing import Any

import yaml

from asimoov.contracts.audio import AudioSink, AudioSource
from asimoov.contracts.behaviors import BehaviorResult, BehaviorStatus
from asimoov.contracts.body import Body, BodyContext, BodyHealth, BodyManifest, GazeTarget
from asimoov.contracts.face import FaceGaze, FaceState
from asimoov.contracts.vocab import TOPICS, is_emotion

logger = logging.getLogger(__name__)

MANIFEST_PATH = Path(__file__).parent / "manifest.yaml"
SRC = "bodies.avatar"
PUBLISHED_HISTORY = 512

GAZE_HZ = 20.0
GAZE_TAU_S = 0.12
GAZE_EPSILON = 0.004
# Slightly longer than the 1/GAZE_HZ publish period, so a continuous
# animation never leaves a gap in which the mind takes the gaze back.
OVERLAY_TTL_S = 0.25
NOD_MS = 900
SHAKE_MS = 900
EXPRESS_MS = 300


def load_manifest(path: Path = MANIFEST_PATH) -> BodyManifest:
    """Load ``manifest.yaml`` into a `BodyManifest`."""
    return BodyManifest.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))


class AvatarBody(Body):
    """Virtual body whose only actuator is the face."""

    def __init__(self, manifest: BodyManifest | None = None) -> None:
        self.manifest = manifest or load_manifest()
        self.published: deque[FaceState] = deque(maxlen=PUBLISHED_HISTORY)
        self.last_stop_all_reason: str | None = None
        self._bus: Any = None
        self._connected = False
        self._face = FaceState(emotion="neutral")
        self._gaze = [0.0, 0.0]
        self._gaze_target = [0.0, 0.0]
        self._anim_offset = [0.0, 0.0]
        self._gaze_task: asyncio.Task[None] | None = None
        self._animation: asyncio.Task[None] | None = None
        self._max_az = float(self.manifest.limits.get("max_gaze_az_deg", 45.0))
        self._max_el = float(self.manifest.limits.get("max_gaze_el_deg", 30.0))

    # ------------------------------------------------------------ lifecycle

    async def start(self, ctx: BodyContext) -> None:
        self._bus = ctx.bus
        self._connected = True
        if self._gaze_task is None:
            self._gaze_task = asyncio.create_task(self._gaze_loop())

    async def stop(self) -> None:
        self._connected = False
        await self._cancel_animation()
        if self._gaze_task is not None:
            self._gaze_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._gaze_task
            self._gaze_task = None

    async def health(self) -> BodyHealth:
        return BodyHealth(connected=self._connected, battery=None)

    # ------------------------------------------------------------ primitives

    async def gesture(self, name: str, params: dict[str, Any], *, timeout_s: float) -> BehaviorResult:
        spec = self.manifest.implements.get(name)
        if spec is None:
            return BehaviorResult.unsupported(f"{name} is not implemented by the avatar")
        primitive = spec["primitive"]
        if primitive == "gaze":
            await self.look_at(GazeTarget(az=float(params.get("az", 0.0)), el=float(params.get("el", 0.0))))
            return BehaviorResult.ok()
        if primitive != "face_anim":
            return BehaviorResult.unsupported(f"unknown primitive {primitive!r}")

        animation = spec.get("arg", name)
        if animation == "express":
            emotion = params.get("emotion", "neutral")
            if not is_emotion(emotion):
                return BehaviorResult.error(f"unknown emotion: {emotion!r}")
            self._face = FaceState(
                emotion=emotion,
                intensity=float(params.get("intensity", 1.0)),
                gaze=self._face.gaze,
                lip=self._face.lip,
                eyelids=self._face.eyelids,
                talking=self._face.talking,
            )
            result = await self._timed(self._express_animation(), timeout_s)
            if result.status is not BehaviorStatus.OK:
                return result
            return BehaviorResult.ok(emotion=emotion)

        if animation == "nod":
            return await self._timed(self._head_animation(NOD_MS, axis=1), timeout_s)
        if animation == "shake_head":
            return await self._timed(self._head_animation(SHAKE_MS, axis=0), timeout_s)
        return BehaviorResult.unsupported(f"unknown animation {animation!r}")

    async def move(self, vx: float, vy: float, wz: float, duration_s: float) -> BehaviorResult:
        return BehaviorResult.unsupported("the avatar has no locomotion")

    async def look_at(self, target: GazeTarget) -> None:
        self._gaze_target = [
            _clamp(target.az / self._max_az, -1.0, 1.0),
            _clamp(target.el / self._max_el, -1.0, 1.0),
        ]

    async def set_face(self, state: FaceState) -> None:
        self._face = state

    async def stop_all(self, reason: str) -> None:
        self.last_stop_all_reason = reason
        self._connected = False
        self._gaze_target = [0.0, 0.0]
        self._anim_offset = [0.0, 0.0]
        await self._cancel_animation()

    async def simulate_disconnect(self) -> None:
        """Simulate losing the face page, as `SafetyGuard` would see it."""
        await self.stop_all("simulated_disconnect")

    def audio_source(self) -> AudioSource | None:
        return None

    def audio_sink(self) -> AudioSink | None:
        return None

    # ------------------------------------------------------------ animation

    def current_face(self) -> FaceState:
        """The `FaceState` the avatar shows right now (base state + gaze)."""
        return FaceState(
            emotion=self._face.emotion,
            intensity=self._face.intensity,
            gaze=FaceGaze(
                x=_clamp(self._gaze[0] + self._anim_offset[0], -1.0, 1.0),
                y=_clamp(self._gaze[1] + self._anim_offset[1], -1.0, 1.0),
            ),
            lip=self._face.lip,
            blink=False,
            eyelids=self._face.eyelids,
            talking=self._face.talking,
            ts=time.time(),
        )

    async def _timed(self, coro: Any, timeout_s: float) -> BehaviorResult:
        """Run ``coro`` as the current animation and report its real outcome.

        The animation runs as a task so `stop_all` can cancel it mid-flight;
        a cancelled animation is an ``error``, never a fabricated success.
        """
        await self._cancel_animation()
        task = asyncio.create_task(coro)
        self._animation = task
        done, _pending = await asyncio.wait({task}, timeout=timeout_s)
        if not done:
            await self._cancel_animation()
            return BehaviorResult.timeout()
        self._animation = None
        if task.cancelled():
            return BehaviorResult.error("animation stopped")
        error = task.exception()
        if error is not None:
            return BehaviorResult.error(f"{type(error).__name__}: {error}")
        return BehaviorResult.ok()

    async def _cancel_animation(self) -> None:
        if self._animation is None:
            return
        task, self._animation = self._animation, None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("avatar animation failed")

    async def _express_animation(self) -> None:
        await self._publish()
        await asyncio.sleep(EXPRESS_MS / 1000)

    async def _head_animation(self, duration_ms: int, *, axis: int) -> None:
        """Two sine swings of the gaze on ``axis`` (0 = x shake, 1 = y nod)."""
        steps = int(duration_ms / 1000 * GAZE_HZ)
        try:
            for step in range(steps + 1):
                phase = step / steps
                self._anim_offset[axis] = 0.45 * math.sin(phase * 4 * math.pi)
                await self._publish()
                await asyncio.sleep(1.0 / GAZE_HZ)
        finally:
            self._anim_offset[axis] = 0.0

    async def _gaze_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0 / GAZE_HZ)
            k = 1 - math.exp(-(1.0 / GAZE_HZ) / GAZE_TAU_S)
            moved = False
            for axis in (0, 1):
                delta = (self._gaze_target[axis] - self._gaze[axis]) * k
                if abs(delta) > GAZE_EPSILON:
                    self._gaze[axis] += delta
                    moved = True
            if moved and self._animation is None:
                await self._publish()

    async def _publish(self) -> None:
        """Offer the gaze the avatar is showing to the mind's face mixer."""
        state = self.current_face()
        self.published.append(state)
        if self._bus is not None:
            await self._bus.publish(
                TOPICS.FACE_OVERLAY,
                {"gaze": {"x": state.gaze.x, "y": state.gaze.y}, "ttl_s": OVERLAY_TTL_S},
                kind="state",
            )


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value
