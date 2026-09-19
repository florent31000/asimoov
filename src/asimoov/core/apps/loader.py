"""Loading apps: entry points, folder manifests, permissions, degraded mode.

Permissions are denied by default and granted per app in
``robot.yaml: apps_permissions``; a missing capability activates the app's
own ``degraded`` rules (disable these tools, tell the persona why) instead
of pretending the app works (plan.md section 4.3). No store, no signature.
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path

import yaml

from asimoov.contracts.app_manifest import AppManifest

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "asimoov.apps"
MANIFEST_FILENAME = "asimoov-app.yaml"


class AppLoadError(ValueError):
    """Raised when an app manifest is malformed or an entry point is unusable.

    The message always names the app and the file or entry point at fault.
    """


@dataclass(frozen=True)
class LoadedApp:
    """One app after permission and capability checks."""

    manifest: AppManifest
    source: str
    loaded: bool = True
    granted: tuple[str, ...] = ()
    denied: tuple[str, ...] = ()
    disabled_tools: tuple[str, ...] = ()
    fragments: tuple[str, ...] = ()
    reason: str | None = None
    factory: object | None = None
    degraded: bool = field(default=False)

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(
            tool for tool in self.manifest.provides.tools if tool not in self.disabled_tools
        )


def _manifest_from_dict(payload, source: str) -> AppManifest:
    if not isinstance(payload, dict):
        raise AppLoadError(f"{source}: expected a YAML mapping at the top level")
    if payload.get("apiVersion") != "asimoov/v1":
        raise AppLoadError(
            f"{source}: 'apiVersion' must be 'asimoov/v1', got {payload.get('apiVersion')!r}"
        )
    if payload.get("kind") != "App":
        raise AppLoadError(f"{source}: 'kind' must be 'App', got {payload.get('kind')!r}")
    try:
        return AppManifest.from_dict(payload)
    except KeyError as exc:
        raise AppLoadError(f"{source}: missing required field {exc}") from exc


def _load_folder_manifest(path: Path) -> AppManifest:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AppLoadError(f"{path}: cannot read app manifest ({exc})") from exc
    except yaml.YAMLError as exc:
        raise AppLoadError(f"{path}: invalid YAML ({exc})") from exc
    return _manifest_from_dict(raw, str(path))


def _entry_point_manifest(target, source: str) -> tuple[AppManifest, object]:
    manifest = getattr(target, "manifest", None)
    if manifest is None:
        raise AppLoadError(
            f"{source}: entry point object has no 'manifest' attribute (expected an "
            "AppManifest or the app.v1 mapping)"
        )
    if isinstance(manifest, AppManifest):
        return manifest, target
    if isinstance(manifest, dict):
        return _manifest_from_dict(manifest, source), target
    raise AppLoadError(f"{source}: 'manifest' must be an AppManifest or a mapping")


def discover(names: tuple[str, ...], search_dirs: tuple[Path, ...]) -> dict[str, tuple[AppManifest, str, object | None]]:
    """Find each requested app, by entry point first then by folder manifest."""
    found: dict[str, tuple[AppManifest, str, object | None]] = {}
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        if entry.name not in names or entry.name in found:
            continue
        source = f"entry point {ENTRY_POINT_GROUP}:{entry.name}"
        manifest, target = _entry_point_manifest(entry.load(), source)
        found[entry.name] = (manifest, source, target)

    for directory in search_dirs:
        for name in names:
            if name in found:
                continue
            candidate = Path(directory) / name / MANIFEST_FILENAME
            if candidate.is_file():
                found[name] = (_load_folder_manifest(candidate), str(candidate), None)
    return found


def _python_ok(requirement: str, app_name: str) -> bool:
    match = re.fullmatch(r">=\s*(\d+)\.(\d+)", requirement.strip())
    if match is None:
        raise AppLoadError(
            f"{app_name}: requires.python must look like '>=3.10', got {requirement!r}"
        )
    return sys.version_info[:2] >= (int(match.group(1)), int(match.group(2)))


def load_apps(
    names: tuple[str, ...],
    *,
    capabilities: tuple[str, ...] | frozenset[str] = (),
    permissions: dict[str, tuple[str, ...]] | None = None,
    search_dirs: tuple[Path, ...] = (),
) -> list[LoadedApp]:
    """Load the apps named in `robot.yaml`, applying permissions and degraded rules."""
    available = frozenset(capabilities)
    granted_map = permissions or {}
    discovered = discover(tuple(names), tuple(search_dirs))
    loaded: list[LoadedApp] = []

    for name in names:
        entry = discovered.get(name)
        if entry is None:
            log.warning("app %s not found (no entry point, no %s)", name, MANIFEST_FILENAME)
            loaded.append(
                LoadedApp(
                    manifest=AppManifest(name=name, version="0", description="", entrypoint=""),
                    source="not found",
                    loaded=False,
                    reason=f"app {name!r} not found",
                )
            )
            continue

        manifest, source, factory = entry
        if not _python_ok(manifest.requires.python, name):
            loaded.append(
                LoadedApp(
                    manifest=manifest,
                    source=source,
                    loaded=False,
                    reason=f"needs Python {manifest.requires.python}",
                )
            )
            continue

        requested = tuple(manifest.permissions)
        granted = tuple(p for p in requested if p in granted_map.get(name, ()))
        denied = tuple(p for p in requested if p not in granted)

        missing = tuple(
            capability
            for capability in manifest.requires.capabilities
            if capability not in available
        )
        disabled: list[str] = []
        fragments: list[str] = []
        uncovered: list[str] = []
        for capability in missing:
            rules = [rule for rule in manifest.degraded if rule.when_missing == capability]
            if not rules:
                uncovered.append(capability)
                continue
            for rule in rules:
                disabled.extend(rule.disable)
                if rule.persona_fragment:
                    fragments.append(rule.persona_fragment)

        if uncovered:
            loaded.append(
                LoadedApp(
                    manifest=manifest,
                    source=source,
                    loaded=False,
                    granted=granted,
                    denied=denied,
                    reason=f"body lacks {', '.join(uncovered)} and the app declares no degraded rule",
                )
            )
            continue

        if manifest.provides.persona_fragment:
            fragments.insert(0, manifest.provides.persona_fragment)
        if denied:
            log.info("app %s loaded without permission(s): %s", name, ", ".join(denied))

        loaded.append(
            LoadedApp(
                manifest=manifest,
                source=source,
                loaded=True,
                granted=granted,
                denied=denied,
                disabled_tools=tuple(dict.fromkeys(disabled)),
                fragments=tuple(fragments),
                factory=factory,
                degraded=bool(disabled or denied),
            )
        )
    return loaded
