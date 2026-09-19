"""Perception module contract: one capability of the separate perception process.

`python -m asimoov.perception` (WS3) hosts one or more `PerceptionModule`
instances (face_id, VAD, direction) and talks to the hub like any other
`BusClient`. See plan.md section 4.7.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from asimoov.contracts.bus import Bus


@dataclass(frozen=True)
class PerceptionContext:
    """Everything a `PerceptionModule` needs from the runtime to start.

    Mirrors `contracts.body.BodyContext`: ``publish``/``subscribe``/
    ``request`` are plain callables matching the bus API of plan.md
    section 4.3, kept untyped as convenience passthroughs for callers
    written before ``bus`` existed. ``bus`` is the same runtime object,
    typed as `contracts.bus.Bus`.
    """

    config: dict[str, Any] = field(default_factory=dict)
    publish: Any = None
    subscribe: Any = None
    request: Any = None
    bus: Bus | None = None


class PerceptionModule(ABC):
    """A perception capability (e.g. face_id, vad_module, direction_stub)."""

    @abstractmethod
    async def start(self, ctx: PerceptionContext) -> None:
        """Start producing percepts.

        Must not block for more than 100 ms: camera/model initialization
        runs in an internal task. Percepts are published via
        ``ctx.publish`` as they are produced; frames are never stored to
        disk and never leave this process as raw pixels except over
        binary ``frame.*`` topics the core never subscribes to.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop capture/inference and release camera/model resources.

        Idempotent: safe to call when already stopped.
        """

    @abstractmethod
    async def handle_command(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        """Handle a `cmd` addressed to this module (e.g. ``face_id.enroll``).

        Returns the payload used to build the matching `reply` envelope,
        e.g. ``{"ok": True, "samples": 5}``. Real failures (camera lost,
        no face detected within the collection window) are returned as
        ``{"ok": False, "reason": "..."}``, never raised as an opaque
        exception that would leave the caller's `request()` to time out.

        Raises:
            KeyError: if ``name`` is not a command this module handles.
        """
