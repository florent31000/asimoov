"""Loading behavior manifests and resolving them against a body's capabilities.

A behavior whose ``requires`` is not covered by the body is not simply
dropped: its ``fallback`` chain is followed, and only a behavior that
resolves to nothing at all is hidden from the LLM (plan.md section 4.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from asimoov.contracts.behaviors import BehaviorManifest
from asimoov.contracts.body import BodyManifest

BUILTIN_DIR = Path(__file__).parent / "builtin"
FALLBACK_SAY = "say"
FALLBACK_NONE = "none"
MAX_FALLBACK_DEPTH = 8


class BehaviorLoadError(ValueError):
    """Raised when a behavior YAML file is missing, malformed, or invalid.

    The message always names the file and the field at fault.
    """


def _load_file(path: Path) -> BehaviorManifest:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BehaviorLoadError(f"{path}: cannot read behavior file ({exc})") from exc
    except yaml.YAMLError as exc:
        raise BehaviorLoadError(f"{path}: invalid YAML ({exc})") from exc

    if not isinstance(raw, dict):
        raise BehaviorLoadError(f"{path}: expected a YAML mapping at the top level")
    if raw.get("apiVersion") != "asimoov/v1":
        raise BehaviorLoadError(
            f"{path}: 'apiVersion' must be 'asimoov/v1', got {raw.get('apiVersion')!r}"
        )
    if raw.get("kind") != "Behavior":
        raise BehaviorLoadError(f"{path}: 'kind' must be 'Behavior', got {raw.get('kind')!r}")
    try:
        return BehaviorManifest.from_dict(raw)
    except KeyError as exc:
        raise BehaviorLoadError(f"{path}: missing required field {exc}") from exc
    except ValueError as exc:
        raise BehaviorLoadError(f"{path}: {exc}") from exc


def load_behavior_dir(directory: str | Path) -> dict[str, BehaviorManifest]:
    """Load every `*.yaml` behavior in ``directory`` (non-recursive).

    Raises:
        BehaviorLoadError: on an unreadable/invalid file, or on two files
            declaring the same behavior name.
    """
    path = Path(directory)
    if not path.is_dir():
        raise BehaviorLoadError(f"{path}: not a behavior directory")
    manifests: dict[str, BehaviorManifest] = {}
    for file in sorted([*path.glob("*.yaml"), *path.glob("*.yml")]):
        manifest = _load_file(file)
        if manifest.name in manifests:
            raise BehaviorLoadError(f"{file}: behavior {manifest.name!r} is already defined")
        manifests[manifest.name] = manifest
    return manifests


def load_builtin_behaviors() -> dict[str, BehaviorManifest]:
    """Load the behaviors shipped with the core (`core/behaviors/builtin`)."""
    return load_behavior_dir(BUILTIN_DIR)


def load_behaviors(*directories: str | Path | None) -> dict[str, BehaviorManifest]:
    """Builtin behaviors, overridden by each directory in order."""
    manifests = load_builtin_behaviors()
    for directory in directories:
        if directory is None:
            continue
        manifests.update(load_behavior_dir(directory))
    return manifests


@dataclass(frozen=True)
class Resolution:
    """What actually happens when a behavior is requested.

    ``mode`` is ``direct`` (the body can do it), ``fallback`` (a substitute
    behavior runs instead), ``say`` (nothing physical: the answer is the
    robot's own speech) or ``hidden`` (impossible, never offered to the LLM).
    """

    requested: str
    mode: str
    manifest: BehaviorManifest | None = None
    reason: str | None = None

    @property
    def runnable(self) -> bool:
        return self.mode in ("direct", "fallback", "say")


class BehaviorResolver:
    """Maps a behavior name to what this body can actually run."""

    def __init__(
        self,
        manifests: dict[str, BehaviorManifest],
        capabilities: tuple[str, ...] | frozenset[str],
        implements: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.manifests = dict(manifests)
        self.capabilities = frozenset(capabilities)
        self.implements = dict(implements or {})

    @classmethod
    def from_body(
        cls, manifests: dict[str, BehaviorManifest], body: BodyManifest
    ) -> BehaviorResolver:
        return cls(manifests, body.capabilities, body.implements)

    def satisfied(self, manifest: BehaviorManifest) -> bool:
        """True if the body declares every capability the behavior requires."""
        return set(manifest.requires) <= self.capabilities

    def primitive(self, name: str) -> dict[str, Any] | None:
        """The body's ``implements`` entry for ``name``, or None."""
        return self.implements.get(name)

    def resolve(self, name: str) -> Resolution:
        """Resolve ``name`` through its fallback chain."""
        manifest = self.manifests.get(name)
        if manifest is None:
            return Resolution(name, "hidden", reason=f"unknown behavior {name!r}")

        seen: set[str] = set()
        current = manifest
        depth = 0
        while True:
            if self.satisfied(current):
                mode = "direct" if current.name == name else "fallback"
                return Resolution(name, mode, manifest=current)

            fallback = current.fallback
            depth += 1
            if not fallback or fallback == FALLBACK_NONE or depth > MAX_FALLBACK_DEPTH:
                missing = sorted(set(current.requires) - self.capabilities)
                return Resolution(
                    name,
                    "hidden",
                    reason=f"body lacks {', '.join(missing)}" if missing else "no fallback",
                )
            if fallback == FALLBACK_SAY:
                return Resolution(name, "say", manifest=manifest)
            if fallback in seen:
                return Resolution(name, "hidden", reason=f"fallback cycle at {fallback!r}")
            seen.add(fallback)
            nxt = self.manifests.get(fallback)
            if nxt is None:
                return Resolution(name, "hidden", reason=f"unknown fallback {fallback!r}")
            current = nxt

    def visible(self) -> tuple[BehaviorManifest, ...]:
        """Every LLM-visible behavior this body can honour, directly or via fallback."""
        return tuple(
            manifest
            for manifest in self.manifests.values()
            if manifest.llm_visible and self.resolve(manifest.name).runnable
        )
