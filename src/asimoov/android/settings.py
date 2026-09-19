"""The three things the phone has to be told, stored outside the APK.

`getFilesDir()/secrets.yaml` is private app storage: it is not in the APK, not
in the repository, and not readable by other apps. The settings screen writes
it; nothing else does.

Import-guarded: off Android the file lives in `~/.asimoov/secrets.yaml` so the
same code runs on a laptop.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from asimoov.core.config import asimoov_home

log = logging.getLogger(__name__)

FILENAME = "secrets.yaml"
ROLES = ("core", "head", "face_only")

try:  # pragma: no cover - only importable inside the APK
    from jnius import autoclass

    ON_ANDROID = True
except ImportError:  # pragma: no cover - the normal case off-device
    autoclass = None
    ON_ANDROID = False


@dataclass
class Settings:
    """What the settings screen collects. Never committed, never in the APK."""

    api_key: str = ""
    body_address: str = ""
    name: str = ""
    role: str = "core"

    def is_complete(self) -> bool:
        if self.role not in ROLES:
            return False
        if self.role == "core":
            return bool(self.api_key)
        return bool(self.body_address)


def settings_dir() -> Path:
    if ON_ANDROID:
        return Path(autoclass("org.kivy.android.PythonActivity").mActivity.getFilesDir().getAbsolutePath())
    return asimoov_home()


def settings_path() -> Path:
    return settings_dir() / FILENAME


def load() -> Settings:
    path = settings_path()
    if not path.is_file():
        return Settings()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a mapping")
    known = {f: data.get(f, getattr(Settings, f)) for f in Settings.__dataclass_fields__}
    return Settings(**known)


def save(settings: Settings) -> Path:
    if settings.role not in ROLES:
        raise ValueError(f"unknown role: {settings.role!r} (expected one of {ROLES})")
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(asdict(settings), sort_keys=True), encoding="utf-8")
    os.chmod(path, 0o600)
    log.info("settings written to %s", path)
    return path
