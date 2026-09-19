"""`BehaviorExecutor`: turns a resolved behavior into real body primitives.

Neon fabricated tool results and let sport commands hang forever (plan.md
section 2, items 7 and 8). Here every ``instant``/``short`` behavior is
awaited under a timeout and returns the body's real answer, and every
``long`` behavior returns ``started`` immediately, its outcome arriving
later as a mind injection.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from asimoov.contracts.behaviors import (
    BehaviorCall,
    BehaviorManifest,
    BehaviorResult,
    BehaviorStatus,
)
from asimoov.contracts.body import Body, GazeTarget
from asimoov.contracts.face import FaceState
from asimoov.contracts.vocab import is_emotion
from asimoov.core.behaviors.resolver import BehaviorResolver
from asimoov.core.telemetry import Telemetry
from asimoov.core.timeouts import run_with_timeout

log = logging.getLogger(__name__)

SYNC_TIMEOUT_S = 2.0
DEFAULT_LONG_ETA_S = 4.0
DEFAULT_MOVE_DURATION_S = 1.0
GESTURE_PRIMITIVES = ("gesture", "sport")
GAZE_PRIMITIVES = ("gaze", "gaze_yaw", "gaze_pan_tilt")
FACE_PRIMITIVES = ("face", "expression")

CompletionHook = Callable[[str, str, str], Awaitable[None] | None]


class BehaviorExecutor:
    """Runs behaviors on a `Body`, tracking the long ones still in flight."""

    def __init__(
        self,
        body: Body,
        resolver: BehaviorResolver,
        *,
        telemetry: Telemetry | None = None,
        on_completion: CompletionHook | None = None,
        max_continuous_motion_s: float | None = None,
    ) -> None:
        self.body = body
        self.resolver = resolver
        self.telemetry = telemetry
        self.on_completion = on_completion
        self.max_continuous_motion_s = max_continuous_motion_s or body.manifest.limits.get(
            "max_continuous_motion_s"
        )
        self.active: dict[str, tuple[str, asyncio.Task]] = {}

    async def run(self, call: BehaviorCall, *, timeout_s: float | None = None) -> BehaviorResult:
        """Execute ``call``, or explain why this body cannot."""
        resolution = self.resolver.resolve(call.name)
        if not resolution.runnable:
            return BehaviorResult.unsupported(resolution.reason or f"{call.name} is unavailable")
        if resolution.mode == "say":
            return BehaviorResult.ok(spoken_only=True, behavior=call.name)

        manifest = resolution.manifest
        assert manifest is not None
        spec = self.resolver.primitive(manifest.name) or self.resolver.primitive(call.name)
        if spec is None:
            if manifest.requires:
                return BehaviorResult.error(
                    f"body declares {', '.join(manifest.requires)} but does not implement "
                    f"{manifest.name!r}"
                )
            return BehaviorResult.ok(spoken_only=True, behavior=manifest.name)

        if manifest.duration_class == "long":
            return self._start_long(call, manifest, spec)
        return await self._run_sync(call, manifest, spec, timeout_s)

    async def cancel_all(self, reason: str) -> None:
        """Cancel every long behavior still running (e-stop, new turn, shutdown)."""
        for action_id, (name, task) in list(self.active.items()):
            task.cancel()
            self.active.pop(action_id, None)
            log.info("behavior %s (%s) canceled: %s", name, action_id, reason)
            await self._notify(action_id, name, "canceled")

    # -- internals --------------------------------------------------------

    async def _run_sync(
        self,
        call: BehaviorCall,
        manifest: BehaviorManifest,
        spec: dict[str, Any],
        timeout_s: float | None,
    ) -> BehaviorResult:
        budget = timeout_s or SYNC_TIMEOUT_S
        if self.telemetry is not None:
            with self.telemetry.tool_span(manifest.name):
                answered, result = await run_with_timeout(
                    self._dispatch(call, manifest, spec, budget), budget
                )
        else:
            answered, result = await run_with_timeout(
                self._dispatch(call, manifest, spec, budget), budget
            )
        if not answered:
            return BehaviorResult.timeout(f"{manifest.name} did not answer within {budget}s")
        return result

    def _start_long(
        self, call: BehaviorCall, manifest: BehaviorManifest, spec: dict[str, Any]
    ) -> BehaviorResult:
        eta_s = float(spec.get("est_ms", DEFAULT_LONG_ETA_S * 1000)) / 1000
        action_id = call.call_id
        task = asyncio.create_task(
            self._run_long(action_id, call, manifest, spec, eta_s),
            name=f"behavior:{manifest.name}:{action_id}",
        )
        self.active[action_id] = (manifest.name, task)
        return BehaviorResult.started(action_id, eta_s)

    async def _run_long(
        self,
        action_id: str,
        call: BehaviorCall,
        manifest: BehaviorManifest,
        spec: dict[str, Any],
        eta_s: float,
    ) -> None:
        status = "failed"
        try:
            budget = max(eta_s * 3, SYNC_TIMEOUT_S)
            answered, result = await run_with_timeout(
                self._dispatch(call, manifest, spec, budget), budget
            )
            if not answered:
                log.warning("behavior %s timed out", manifest.name)
            else:
                status = "done" if result.status is BehaviorStatus.OK else "failed"
                if result.status is not BehaviorStatus.OK:
                    log.warning("behavior %s failed: %s", manifest.name, result.reason)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("behavior %s raised", manifest.name)
        finally:
            self.active.pop(action_id, None)
        await self._notify(action_id, manifest.name, status)

    async def _notify(self, action_id: str, name: str, status: str) -> None:
        if self.on_completion is None:
            return
        outcome = self.on_completion(action_id, name, status)
        if asyncio.iscoroutine(outcome):
            await outcome

    async def _dispatch(
        self,
        call: BehaviorCall,
        manifest: BehaviorManifest,
        spec: dict[str, Any],
        budget: float,
    ) -> BehaviorResult:
        primitive = spec.get("primitive")
        params = dict(call.params)

        if primitive in GESTURE_PRIMITIVES:
            # The behavior name, not ``spec["arg"]``: a body looks the
            # behavior up in its own `implements` to find its arg (see
            # `bodies.conformance._check_implements_executable`). Passing
            # ``arg`` made every Go2 gesture fail with "no sport command for
            # gesture 'Sit'", since the Go2 keys on ``sit``.
            return await self.body.gesture(manifest.name, params, timeout_s=budget)
        if primitive == "move":
            return await self._move(params)
        if primitive == "turn":
            return await self._turn(params)
        if primitive in GAZE_PRIMITIVES:
            await self.body.look_at(
                GazeTarget(
                    az=float(params.get("az", 0.0)),
                    el=float(params.get("el", 0.0)),
                    track_id=params.get("track_id"),
                )
            )
            return BehaviorResult.ok()
        if primitive in FACE_PRIMITIVES:
            emotion = params.get("emotion", "neutral")
            if not is_emotion(emotion):
                return BehaviorResult.error(f"unknown emotion {emotion!r}")
            await self.body.set_face(
                FaceState(emotion=emotion, intensity=float(params.get("intensity", 1.0)))
            )
            return BehaviorResult.ok()
        if primitive == "stop_all":
            await self.body.stop_all(params.get("reason", "requested"))
            return BehaviorResult.ok()
        if primitive == "none":
            return BehaviorResult.ok(spoken_only=True, behavior=manifest.name)
        return BehaviorResult.error(f"unknown primitive {primitive!r} for {manifest.name!r}")

    async def _move(self, params: dict[str, Any]) -> BehaviorResult:
        direction = params.get("direction", "forward")
        speed = float(params.get("speed", 0.5))
        duration_s = float(params.get("duration", DEFAULT_MOVE_DURATION_S))
        if self.max_continuous_motion_s:
            duration_s = min(duration_s, float(self.max_continuous_motion_s))
        vectors = {
            "forward": (speed, 0.0, 0.0),
            "backward": (-speed, 0.0, 0.0),
            "left": (0.0, speed, 0.0),
            "right": (0.0, -speed, 0.0),
        }
        vector = vectors.get(direction)
        if vector is None:
            return BehaviorResult.error(f"unknown direction {direction!r}")
        return await self.body.move(*vector, duration_s)

    async def _turn(self, params: dict[str, Any]) -> BehaviorResult:
        direction = params.get("direction", "left")
        if direction not in ("left", "right"):
            return BehaviorResult.error(f"unknown direction {direction!r}")
        speed = float(params.get("speed", 0.5))
        angle = abs(float(params.get("angle", 90.0)))
        duration_s = float(params.get("duration", max(0.3, angle / 90.0)))
        if self.max_continuous_motion_s:
            duration_s = min(duration_s, float(self.max_continuous_motion_s))
        # Positive wz = counter-clockwise = the robot's own left (REP-103);
        # the body clamps the normalized rate against its own max_yaw_rate.
        wz = speed if direction == "left" else -speed
        return await self.body.move(0.0, 0.0, wz, duration_s)
