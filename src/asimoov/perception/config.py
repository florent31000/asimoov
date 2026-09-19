"""The `perception:` section of `robot.yaml`, and the hub URL it implies."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from asimoov.core.bus.client import DEFAULT_HUB_URL
from asimoov.perception import PerceptionError
from asimoov.perception.face_id.module import DEFAULT_FOV_H_DEG

DEFAULT_CAMERA = "ws_frames"
DEFAULT_HUB_PORT = 7331


@dataclass(frozen=True)
class FaceIdConfig:
    """`perception.face_id` in `robot.yaml`."""

    enabled: bool = False
    camera: str = DEFAULT_CAMERA
    fov_h_deg: float = DEFAULT_FOV_H_DEG
    db: str | None = None


@dataclass(frozen=True)
class PerceptionConfig:
    """Everything the perception process reads from `robot.yaml`."""

    face_id: FaceIdConfig = FaceIdConfig()
    vad_enabled: bool = False
    hub_url: str = DEFAULT_HUB_URL


def _check_keys(section: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(section) - allowed)
    if unknown:
        raise PerceptionError(f"unknown key(s) in {where}: {', '.join(unknown)}")


def _hub_url(document: dict) -> str:
    hub = document.get("hub") or {}
    if not isinstance(hub, dict):
        raise PerceptionError("robot.yaml: `hub` must be a mapping")
    host = str(hub.get("host", "127.0.0.1"))
    # The core binds 0.0.0.0; a client connects to a real address.
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"ws://{host}:{int(hub.get('port', DEFAULT_HUB_PORT))}/bus"


def parse_config(document: dict) -> PerceptionConfig:
    """Build a `PerceptionConfig` from an already-loaded `robot.yaml`.

    Raises:
        PerceptionError: on an unknown or malformed key.
    """
    perception = document.get("perception") or {}
    if not isinstance(perception, dict):
        raise PerceptionError("robot.yaml: `perception` must be a mapping")
    _check_keys(perception, {"face_id", "vad"}, "perception")

    face_id_section = perception.get("face_id") or {}
    _check_keys(face_id_section, {"enabled", "camera", "fov_h_deg", "db"}, "perception.face_id")
    vad_section = perception.get("vad") or {}
    _check_keys(vad_section, {"enabled"}, "perception.vad")

    return PerceptionConfig(
        face_id=FaceIdConfig(
            enabled=bool(face_id_section.get("enabled", False)),
            camera=str(face_id_section.get("camera", DEFAULT_CAMERA)),
            fov_h_deg=float(face_id_section.get("fov_h_deg", DEFAULT_FOV_H_DEG)),
            db=face_id_section.get("db"),
        ),
        vad_enabled=bool(vad_section.get("enabled", False)),
        hub_url=_hub_url(document),
    )


def load_config(path: Path | str) -> PerceptionConfig:
    """Load and parse `robot.yaml`.

    Raises:
        PerceptionError: if the file is missing or not a YAML mapping.
    """
    file = Path(path)
    if not file.is_file():
        raise PerceptionError(f"config not found: {file}")
    document = yaml.safe_load(file.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise PerceptionError(f"config is not a YAML mapping: {file}")
    return parse_config(document)
