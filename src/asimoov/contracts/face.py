"""Face state and renderer contracts (face_state.v1).

See ``schemas/face_state.v1.json`` and ``examples/face_state.curious.json``.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from asimoov.contracts.vocab import is_emotion


@dataclass(frozen=True)
class FaceGaze:
    """Normalized pupil offset in [-1, 1] on each axis.

    Sign convention (frozen, see ``docs/contracts.md`` "Units and
    conventions"): ``x`` positive = the face looks toward the robot's own
    left, which is the *viewer's right* on a screen facing the viewer
    (mirror convention, like looking someone in the eye); ``y`` positive =
    up. A person at ``bearing.az > 0`` (percepts.Bearing, to the robot's
    left) yields ``gaze.x > 0``.
    """

    x: float
    y: float


@dataclass(frozen=True)
class FaceState:
    """What a face should show right now, published on topic ``face.state``.

    Published at up to 20 Hz while changing, 2 Hz otherwise (plan.md
    section 4.6). ``gaze`` comes from the Scene, ``lip`` from
    ``PlaybackTracker.energy_at_head()``, ``blink`` is driven by the core so
    every renderer blinks in sync.

    Attributes:
        emotion: One of ``vocab.EMOTIONS``.
        intensity: Strength of the emotion, in [0, 1].
        gaze: Normalized pupil offset, each axis in [-1, 1]. See
            `FaceGaze` for the sign convention: x positive = the face
            looks toward the robot's own left, which is the viewer's
            *right* on a screen facing the viewer; y positive = up.
        lip: Mouth opening in [0, 1], from `PlaybackTracker.energy_at_head`.
        blink: True only on the frame that starts a blink, not for its
            whole duration: renderers animate the 150 ms themselves.
        eyelids: Droop in [0, 1] (0 = wide open, 1 = closed).
        talking: True while speech audio is actually playing.
        ts: Unix timestamp in seconds (float), like every ``ts`` in these
            contracts.

    Values outside these ranges are a producer bug; `face_state.v1.json`
    rejects them and renderers may clamp rather than guess.

    Raises:
        ValueError: if ``emotion`` is not in ``vocab.EMOTIONS``.
    """

    emotion: str
    intensity: float = 1.0
    gaze: FaceGaze = field(default_factory=lambda: FaceGaze(0.0, 0.0))
    lip: float = 0.0
    blink: bool = False
    eyelids: float = 0.0
    talking: bool = False
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not is_emotion(self.emotion):
            raise ValueError(f"invalid emotion: {self.emotion!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "emotion": self.emotion,
            "intensity": self.intensity,
            "gaze": {"x": self.gaze.x, "y": self.gaze.y},
            "lip": self.lip,
            "blink": self.blink,
            "eyelids": self.eyelids,
            "talking": self.talking,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> FaceState:
        gaze = payload.get("gaze", {"x": 0.0, "y": 0.0})
        return cls(
            emotion=payload["emotion"],
            intensity=payload.get("intensity", 1.0),
            gaze=FaceGaze(x=gaze.get("x", 0.0), y=gaze.get("y", 0.0)),
            lip=payload.get("lip", 0.0),
            blink=payload.get("blink", False),
            eyelids=payload.get("eyelids", 0.0),
            talking=payload.get("talking", False),
            ts=payload.get("ts", time.time()),
        )


class FaceRenderer(ABC):
    """Renders a `FaceState` somewhere: web canvas, Kivy widget, servo/LEDs."""

    @abstractmethod
    async def start(self, ctx: dict[str, Any]) -> None:
        """Start the renderer. Must not block for more than 100 ms.

        ``ctx`` carries renderer-specific config (e.g. the hub host/port for
        `WebFace`, the servo link for `ServoFace`).
        """

    @abstractmethod
    async def render(self, state: FaceState) -> None:
        """Display ``state``. Called at up to 25 Hz; must never block on I/O

        long enough to fall behind the caller (drop frames instead of
        queuing if rendering cannot keep up).
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the renderer and release any resources. Idempotent."""


@dataclass(frozen=True)
class ProcessRequestResponse:
    """A plain HTTP response, used to short-circuit a WebSocket handshake."""

    status: int
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""


class ProcessRequestHook(Protocol):
    """Hook implemented by WS6 (`faces.server`) and wired by WS1's hub.

    The hub serves everything on a single port (7331): normal bus WebSocket
    connections, and the face page over plain HTTP. Before completing a
    WebSocket handshake, the server calls
    ``response = hook(path)`` (or awaits it, if the implementation is a
    coroutine function); returning a `ProcessRequestResponse` answers the
    request as plain HTTP (e.g. serving ``faces/web/index.html``), while
    returning None lets the connection proceed as a normal WebSocket
    upgrade.
    """

    def __call__(
        self, path: str
    ) -> ProcessRequestResponse | None | Awaitable[ProcessRequestResponse | None]: ...


ProcessRequestFn = Callable[
    [str], "ProcessRequestResponse | None | Awaitable[ProcessRequestResponse | None]"
]
