"""Body contract (body.v1): the ABC every physical or virtual body implements.

A body never receives raw LLM output. It only executes primitives dispatched
by the core's `Resolver`/`Executor` (owned by WS1). See
``schemas/body.v1.json``, ``examples/body.go2.yaml`` and plan.md section 4.5.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from asimoov.contracts.audio import AudioSink, AudioSource
from asimoov.contracts.behaviors import BehaviorResult
from asimoov.contracts.bus import Bus
from asimoov.contracts.face import FaceState
from asimoov.contracts.vocab import BODY_KINDS, is_capability


@dataclass(frozen=True)
class BodyManifest:
    """Static description of a body, loaded from `bodies/<name>/manifest.yaml`.

    ``implements`` maps a behavior name to how this body performs it, e.g.
    ``{"wave_hello": {"primitive": "sport", "arg": "Hello", "est_ms": 4000}}``.
    The `BehaviorResolver` (WS1) only exposes to the LLM the behaviors whose
    ``requires`` is a subset of ``capabilities``.

    Raises:
        ValueError: if ``kind_of_body`` is not in ``vocab.BODY_KINDS``, or if
            any entry of ``capabilities`` is not a known capability
            (``vocab.is_capability``).
    """

    name: str
    kind_of_body: str
    capabilities: tuple[str, ...] = ()
    implements: dict[str, dict[str, Any]] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    safety: dict[str, Any] = field(default_factory=dict)
    sensors: tuple[str, ...] = ()
    api_version: str = "asimoov/v1"

    def __post_init__(self) -> None:
        if self.kind_of_body not in BODY_KINDS:
            raise ValueError(f"invalid kind_of_body: {self.kind_of_body!r}")
        for capability in self.capabilities:
            if not is_capability(capability):
                raise ValueError(f"unknown capability: {capability!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "apiVersion": self.api_version,
            "kind": "Body",
            "name": self.name,
            "kind_of_body": self.kind_of_body,
            "capabilities": list(self.capabilities),
            "implements": self.implements,
            "limits": self.limits,
            "safety": self.safety,
            "sensors": list(self.sensors),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> BodyManifest:
        return cls(
            name=payload["name"],
            kind_of_body=payload["kind_of_body"],
            capabilities=tuple(payload.get("capabilities", [])),
            implements=payload.get("implements", {}),
            limits=payload.get("limits", {}),
            safety=payload.get("safety", {}),
            sensors=tuple(payload.get("sensors", [])),
            api_version=payload.get("apiVersion", "asimoov/v1"),
        )


@dataclass(frozen=True)
class GazeTarget:
    """Where a body should look, in body-relative angles (degrees).

    Attributes:
        az: Azimuth in degrees, same convention and sign as
            ``percepts.Bearing.az`` (0 = straight ahead, positive = to the
            robot's own left, counter-clockwise seen from above, ROS
            REP-103). A body whose ``look_at`` drives a yaw joint turns
            that joint left for ``az > 0``.
        el: Elevation in degrees (0 = horizon, positive = up).
        track_id: Track this target came from, or None for a free look.
        urgency: How fast to get there, in [0, 1]; the body maps it to its
            own slew rate.
    """

    az: float
    el: float
    track_id: str | None = None
    urgency: float = 0.5


@dataclass(frozen=True)
class BodyContext:
    """Everything a `Body` needs from the runtime to start.

    ``publish``/``subscribe``/``request`` mirror the bus API of plan.md
    section 4.3 (owned by WS1's `core.bus`); they are kept as untyped
    convenience passthroughs for callers written before ``bus`` existed.
    ``bus`` is the same runtime object, typed as `contracts.bus.Bus`.
    """

    config: dict[str, Any] = field(default_factory=dict)
    publish: Any = None
    subscribe: Any = None
    request: Any = None
    bus: Bus | None = None


@dataclass(frozen=True)
class BodyHealth:
    """Snapshot published on topic ``body.health``, polled by `SafetyGuard`.

    Attributes:
        connected: True while the link to the body is up.
        battery: Charge as a fraction in [0, 1] (same unit as the
            ``battery`` percept), or None if the body has no battery sensor.
        last_rtt_ms: Round-trip time of the last command, in milliseconds.
        errors: Human-readable error strings since the last healthy poll.
    """

    connected: bool
    battery: float | None = None
    last_rtt_ms: float | None = None
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "battery": self.battery,
            "last_rtt_ms": self.last_rtt_ms,
            "errors": list(self.errors),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> BodyHealth:
        return cls(
            connected=payload["connected"],
            battery=payload.get("battery"),
            last_rtt_ms=payload.get("last_rtt_ms"),
            errors=tuple(payload.get("errors", [])),
        )


class Body(ABC):
    """A physical or virtual body. One instance per running robot.

    Implementations own their own connection lifecycle and must never let a
    caller block on hardware/network I/O beyond the documented timings:
    `SafetyGuard` (WS1) treats any violation as a fault and calls
    `stop_all`.
    """

    manifest: BodyManifest

    @abstractmethod
    async def start(self, ctx: BodyContext) -> None:
        """Start the body's connection.

        Must not block for more than 100 ms: any real connection attempt
        (WebRTC, serial, TCP) runs in an internal task with its own retry
        / backoff loop, not on this call's critical path.

        Raises:
            Nothing: connection failures are reported via `health()`, not
                by raising, since `start` returns before connecting.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the body's connection and internal tasks. Idempotent."""

    @abstractmethod
    async def health(self) -> BodyHealth:
        """Return the current connection/battery/error snapshot.

        Must not block on network I/O; returns the last known state.
        """

    @abstractmethod
    async def gesture(self, name: str, params: dict[str, Any], *, timeout_s: float) -> BehaviorResult:
        """Run a named gesture primitive (e.g. from `BodyManifest.implements`).

        Waits for the real outcome up to ``timeout_s`` and returns
        ``BehaviorResult(status="ok")`` on success. Never returns a
        fabricated success: an unanswered command after ``timeout_s`` must
        produce ``BehaviorResult.timeout()``, and any other failure
        ``BehaviorResult.error(reason)``. Never raises for expected failures.

        Raises:
            asyncio.TimeoutError: never; timeouts are reported as a
                `BehaviorResult`, not an exception.
        """

    @abstractmethod
    async def move(self, vx: float, vy: float, wz: float, duration_s: float) -> BehaviorResult:
        """Move at normalized velocity ``vx``/``vy``/``wz`` in [-1, 1].

        Values are clamped by `BodyManifest.limits` (``max_speed``,
        ``max_yaw_rate``) before being sent to hardware. This is a ``long``
        duration_class primitive: implementations return
        ``BehaviorResult.started(action_id, eta_s)`` immediately if the
        motion is asynchronous, or the final result once ``duration_s`` has
        elapsed for bodies that can await it directly.
        """

    @abstractmethod
    async def look_at(self, target: GazeTarget) -> None:
        """Point gaze/head/camera toward ``target``. Fire-and-forget.

        Callers must not call this faster than 5 Hz; implementations are
        responsible for smoothing between successive targets themselves.
        Never raises: unreachable targets are silently clamped. See
        `GazeTarget.az` for the sign convention: a yaw joint turns left
        for ``target.az > 0``.
        """

    @abstractmethod
    async def set_face(self, state: FaceState) -> None:
        """Push a face state to this body, if it has a ``face.*`` capability.

        Callers must not call this faster than 25 Hz. No-op if the body has
        no face capability. Never raises.
        """

    @abstractmethod
    async def stop_all(self, reason: str) -> None:
        """Emergency stop: cut all motion immediately.

        Must complete in under 200 ms and be safe to call repeatedly
        (idempotent), including before `start` or after `stop`. Called
        directly by `SafetyGuard` without going through the LLM.
        """

    @abstractmethod
    def audio_source(self) -> AudioSource | None:
        """Return this body's microphone, or None if it has no ``audio.in``."""

    @abstractmethod
    def audio_sink(self) -> AudioSink | None:
        """Return this body's speaker, or None if it has no ``audio.out``."""
