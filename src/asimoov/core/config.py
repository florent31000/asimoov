"""Robot configuration: `robot.yaml` loader, validation, and secrets access."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from asimoov.contracts.persona import Persona, PersonaError, load_persona
from asimoov.contracts.vocab import is_app_permission

DEFAULT_HUB_PORT = 7331
DEFAULT_STOP_PHRASES: tuple[str, ...] = (
    "stop",
    "stop everything",
    "emergency stop",
    "arrete",
    "arrete toi",
    "arrete tout",
)


class ConfigError(ValueError):
    """Raised when a robot configuration is missing, malformed, or inconsistent.

    The message always names the offending file and field.
    """


def asimoov_home() -> Path:
    """User state directory: ``$ASIMOOV_HOME`` if set, else ``~/.asimoov``."""
    raw = os.environ.get("ASIMOOV_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".asimoov"


@dataclass(frozen=True)
class HubConfig:
    host: str = "127.0.0.1"
    port: int = DEFAULT_HUB_PORT
    enabled: bool = True


@dataclass(frozen=True)
class BodyConfig:
    type: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RobotConfig:
    """A whole robot: one `robot.yaml` plus the persona it points at."""

    root: Path
    source: Path
    persona: Persona
    body: BodyConfig
    faces: tuple[str, ...] = ()
    audio: dict[str, Any] = field(default_factory=dict)
    perception: dict[str, Any] = field(default_factory=dict)
    behaviors_dir: Path | None = None
    apps: tuple[str, ...] = ()
    apps_permissions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    hub: HubConfig = field(default_factory=HubConfig)
    stop_phrases: tuple[str, ...] = DEFAULT_STOP_PHRASES
    memory_path: Path | None = None

    def memory_file(self) -> Path:
        return self.memory_path or asimoov_home() / "memory.db"


def _require_mapping(value: Any, source: Path, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{source}: {field_name!r} must be a mapping, got {type(value).__name__}")
    return value


def _require_str_list(value: Any, source: Path, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{source}: {field_name!r} must be a list of strings")
    return tuple(value)


def _load_hub(raw: dict[str, Any], source: Path) -> HubConfig:
    host = raw.get("host", "127.0.0.1")
    port = raw.get("port", DEFAULT_HUB_PORT)
    enabled = raw.get("enabled", True)
    if not isinstance(host, str) or not host:
        raise ConfigError(f"{source}: 'hub.host' must be a non-empty string")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError(f"{source}: 'hub.port' must be an integer in 1..65535, got {port!r}")
    if not isinstance(enabled, bool):
        raise ConfigError(f"{source}: 'hub.enabled' must be a boolean")
    return HubConfig(host=host, port=port, enabled=enabled)


def _load_apps_permissions(raw: Any, source: Path) -> dict[str, tuple[str, ...]]:
    mapping = _require_mapping(raw, source, "apps_permissions")
    granted: dict[str, tuple[str, ...]] = {}
    for app_name, permissions in mapping.items():
        values = _require_str_list(permissions, source, f"apps_permissions.{app_name}")
        unknown = [value for value in values if not is_app_permission(value)]
        if unknown:
            raise ConfigError(
                f"{source}: apps_permissions.{app_name} contains unknown permission(s): "
                f"{', '.join(unknown)}"
            )
        granted[app_name] = values
    return granted


def load_robot_config(path: str | Path) -> RobotConfig:
    """Load and validate a robot directory (or an explicit `robot.yaml` path).

    Raises:
        ConfigError: if the file is missing, is not a `Robot` document of
            `apiVersion: asimoov/v1`, points at an unreadable persona, or
            has a malformed field. Every message names the file and field.
    """
    source = Path(path)
    root = source if source.is_dir() else source.parent
    if source.is_dir():
        source = source / "robot.yaml"
    if not source.is_file():
        raise ConfigError(f"{source}: no such robot configuration file")

    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"{source}: cannot read robot file ({exc})") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: invalid YAML ({exc})") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: expected a YAML mapping at the top level")
    if raw.get("apiVersion") != "asimoov/v1":
        raise ConfigError(
            f"{source}: 'apiVersion' must be 'asimoov/v1', got {raw.get('apiVersion')!r}"
        )
    if raw.get("kind") != "Robot":
        raise ConfigError(f"{source}: 'kind' must be 'Robot', got {raw.get('kind')!r}")

    persona_ref = raw.get("persona")
    if not isinstance(persona_ref, str) or not persona_ref:
        raise ConfigError(f"{source}: 'persona' must be a path to a persona YAML file")
    persona_path = (root / persona_ref).resolve()
    try:
        persona = load_persona(persona_path)
    except PersonaError as exc:
        raise ConfigError(f"{source}: 'persona' -> {exc}") from exc

    body_raw = _require_mapping(raw.get("body"), source, "body")
    body_type = body_raw.get("type")
    if not isinstance(body_type, str) or not body_type:
        raise ConfigError(f"{source}: 'body.type' must be a non-empty string")
    body = BodyConfig(
        type=body_type,
        config=_require_mapping(body_raw.get("config"), source, "body.config"),
    )

    behaviors_dir: Path | None = None
    behaviors_ref = raw.get("behaviors_dir")
    if behaviors_ref is not None:
        if not isinstance(behaviors_ref, str):
            raise ConfigError(f"{source}: 'behaviors_dir' must be a path string")
        behaviors_dir = (root / behaviors_ref).resolve()
        if not behaviors_dir.is_dir():
            raise ConfigError(f"{source}: 'behaviors_dir' does not exist: {behaviors_dir}")

    safety_raw = _require_mapping(raw.get("safety"), source, "safety")
    stop_phrases = _require_str_list(safety_raw.get("stop_phrases"), source, "safety.stop_phrases")

    memory_path: Path | None = None
    memory_ref = raw.get("memory")
    if memory_ref is not None:
        memory_raw = _require_mapping(memory_ref, source, "memory")
        db = memory_raw.get("path")
        if db is not None:
            if not isinstance(db, str):
                raise ConfigError(f"{source}: 'memory.path' must be a path string")
            memory_path = Path(db) if Path(db).is_absolute() else (root / db).resolve()

    return RobotConfig(
        root=root.resolve(),
        source=source.resolve(),
        persona=persona,
        body=body,
        faces=_require_str_list(raw.get("faces"), source, "faces"),
        audio=_require_mapping(raw.get("audio"), source, "audio"),
        perception=_require_mapping(raw.get("perception"), source, "perception"),
        behaviors_dir=behaviors_dir,
        apps=_require_str_list(raw.get("apps"), source, "apps"),
        apps_permissions=_load_apps_permissions(raw.get("apps_permissions"), source),
        hub=_load_hub(_require_mapping(raw.get("hub"), source, "hub"), source),
        stop_phrases=stop_phrases or DEFAULT_STOP_PHRASES,
        memory_path=memory_path,
    )


#: Vendor environment variables accepted as a last resort, after
#: ``$ASIMOOV_<KEY>`` and the secrets file, so that `doctor`, `run` and the
#: voice provider all read the same key (review, major 13).
FALLBACK_ENV_VARS: dict[str, str] = {"openai_api_key": "OPENAI_API_KEY"}


class Secrets:
    """Reads API keys from the environment or `~/.asimoov/secrets.yaml`.

    Lookup order for ``openai_api_key``: ``$ASIMOOV_OPENAI_API_KEY``, then
    the ``openai_api_key`` key of the secrets file, then ``$OPENAI_API_KEY``
    (`FALLBACK_ENV_VARS`). Values are never logged, never printed, and
    never included in an error message.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or asimoov_home() / "secrets.yaml"
        self._file: dict[str, Any] | None = None

    def _from_file(self, key: str) -> str | None:
        if self._file is None:
            if not self.path.is_file():
                self._file = {}
            else:
                try:
                    loaded = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
                except (OSError, yaml.YAMLError) as exc:
                    raise ConfigError(f"{self.path}: cannot read secrets file ({exc})") from exc
                if not isinstance(loaded, dict):
                    raise ConfigError(
                        f"{self.path}: expected a YAML mapping of secret names to values"
                    )
                self._file = loaded
        value = self._file.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ConfigError(f"{self.path}: secret {key!r} must be a string")
        return value

    def env_var(self, key: str) -> str:
        return "ASIMOOV_" + key.upper()

    def get(self, key: str) -> str | None:
        """Return the secret ``key``, or None if no source defines it."""
        fallback = FALLBACK_ENV_VARS.get(key)
        return (
            os.environ.get(self.env_var(key))
            or self._from_file(key)
            or (os.environ.get(fallback) if fallback else None)
        )

    def require(self, key: str) -> str:
        """Return the secret ``key``.

        Raises:
            ConfigError: if it is defined nowhere. The message names the
                environment variable and the file, never a value.
        """
        value = self.get(key)
        if not value:
            fallback = FALLBACK_ENV_VARS.get(key)
            extra = f", or ${fallback}" if fallback else ""
            raise ConfigError(
                f"missing secret {key!r}: set ${self.env_var(key)} or add {key!r} "
                f"to {self.path}{extra}"
            )
        return value
