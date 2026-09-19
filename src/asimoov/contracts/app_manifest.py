"""App manifest contract (app.v1).

See ``schemas/app.v1.json`` and ``examples/app.scene-describer.yaml``.
Loaded via the ``asimoov.apps`` entry point group or an
``apps/<name>/asimoov-app.yaml`` file (plan.md section 4.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AppTrigger:
    """Hints the mind to prefer a tool when an utterance matches."""

    on: str
    match: str
    hint_tool: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"on": self.on, "match": self.match, "hint_tool": self.hint_tool}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AppTrigger:
        return cls(on=payload["on"], match=payload["match"], hint_tool=payload.get("hint_tool"))


@dataclass(frozen=True)
class AppProvides:
    """What an app adds to the running robot."""

    tools: tuple[str, ...] = ()
    behaviors: tuple[str, ...] = ()
    percepts: tuple[str, ...] = ()
    persona_fragment: str = ""
    triggers: tuple[AppTrigger, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "tools": list(self.tools),
            "behaviors": list(self.behaviors),
            "percepts": list(self.percepts),
            "persona_fragment": self.persona_fragment,
            "triggers": [t.to_dict() for t in self.triggers],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AppProvides:
        return cls(
            tools=tuple(payload.get("tools", [])),
            behaviors=tuple(payload.get("behaviors", [])),
            percepts=tuple(payload.get("percepts", [])),
            persona_fragment=payload.get("persona_fragment", ""),
            triggers=tuple(AppTrigger.from_dict(t) for t in payload.get("triggers", [])),
        )


@dataclass(frozen=True)
class AppRequires:
    """What must be true of the robot for this app to load at all."""

    capabilities: tuple[str, ...] = ()
    python: str = ">=3.10"

    def to_dict(self) -> dict[str, Any]:
        return {"capabilities": list(self.capabilities), "python": self.python}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AppRequires:
        return cls(
            capabilities=tuple(payload.get("capabilities", [])),
            python=payload.get("python", ">=3.10"),
        )


@dataclass(frozen=True)
class AppDegraded:
    """What to disable, and what to tell the persona, when a capability is missing."""

    when_missing: str
    disable: tuple[str, ...] = ()
    persona_fragment: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "when_missing": self.when_missing,
            "disable": list(self.disable),
            "persona_fragment": self.persona_fragment,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AppDegraded:
        return cls(
            when_missing=payload["when_missing"],
            disable=tuple(payload.get("disable", [])),
            persona_fragment=payload.get("persona_fragment", ""),
        )


@dataclass(frozen=True)
class AppManifest:
    """Static description of an app, loaded from `asimoov-app.yaml` or an entry point.

    Permissions (``camera.frames``, ``llm.vision``, ``memory.read``, ...) are
    denied by default; a robot grants them explicitly via
    ``robot.yaml: apps_permissions``. There is no store and no signature
    verification in v1.
    """

    name: str
    version: str
    description: str
    entrypoint: str
    permissions: tuple[str, ...] = ()
    provides: AppProvides = field(default_factory=AppProvides)
    requires: AppRequires = field(default_factory=AppRequires)
    degraded: tuple[AppDegraded, ...] = ()
    api_version: str = "asimoov/v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "apiVersion": self.api_version,
            "kind": "App",
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "entrypoint": self.entrypoint,
            "permissions": list(self.permissions),
            "provides": self.provides.to_dict(),
            "requires": self.requires.to_dict(),
            "degraded": [d.to_dict() for d in self.degraded],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AppManifest:
        return cls(
            name=payload["name"],
            version=payload["version"],
            description=payload["description"],
            entrypoint=payload["entrypoint"],
            permissions=tuple(payload.get("permissions", [])),
            provides=AppProvides.from_dict(payload.get("provides", {})),
            requires=AppRequires.from_dict(payload.get("requires", {})),
            degraded=tuple(AppDegraded.from_dict(d) for d in payload.get("degraded", [])),
            api_version=payload.get("apiVersion", "asimoov/v1"),
        )
