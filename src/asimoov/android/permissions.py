"""Runtime permission requests on Android.

Import-guarded: this module imports and its functions are callable on a
laptop, where `is_android()` is False and every request is refused with a
warning rather than silently reported as granted.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

log = logging.getLogger(__name__)

try:  # pragma: no cover - only importable inside the APK
    from android.permissions import Permission, check_permission, request_permissions

    ON_ANDROID = True
except ImportError:  # pragma: no cover - the normal case off-device
    Permission = None
    check_permission = None
    request_permissions = None
    ON_ANDROID = False

# Which Android permissions each phone role actually needs (plan.md 4.10).
# `face_only` needs none: it is a web page in Chrome, not this APK.
ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "core": ("RECORD_AUDIO", "CAMERA", "POST_NOTIFICATIONS"),
    "head": ("RECORD_AUDIO", "CAMERA", "POST_NOTIFICATIONS"),
    "face_only": (),
}


def is_android() -> bool:
    return ON_ANDROID


def _resolve(names: Sequence[str]) -> list[str]:
    resolved = []
    for name in names:
        value = getattr(Permission, name, None)
        if value is None:
            # A permission this Android version does not know (POST_NOTIFICATIONS
            # before API 33) is skipped, not guessed at.
            log.info("permission %s unknown on this Android version, skipping", name)
            continue
        resolved.append(value)
    return resolved


def granted(name: str) -> bool:
    """True if `name` is currently granted. Always False off Android."""
    if not ON_ANDROID:
        return False
    value = getattr(Permission, name, None)
    if value is None:
        return False
    return bool(check_permission(value))


def request(
    names: Sequence[str],
    callback: Callable[[list[str], list[bool]], None] | None = None,
) -> bool:
    """Ask for `names`. Returns False (with a warning) when not on Android."""
    if not ON_ANDROID:
        log.warning("not running on Android: cannot request %s", ", ".join(names))
        return False
    values = _resolve(names)
    if not values:
        return True
    request_permissions(values, callback)
    return True


def request_for_role(role: str, callback=None) -> bool:
    """Ask for everything the given phone role needs."""
    if role not in ROLE_PERMISSIONS:
        raise ValueError(f"unknown role: {role!r} (expected one of {sorted(ROLE_PERMISSIONS)})")
    return request(ROLE_PERMISSIONS[role], callback)


def missing_for_role(role: str) -> list[str]:
    """Permissions the role needs that are not granted yet."""
    if role not in ROLE_PERMISSIONS:
        raise ValueError(f"unknown role: {role!r} (expected one of {sorted(ROLE_PERMISSIONS)})")
    if not ON_ANDROID:
        return list(ROLE_PERMISSIONS[role])
    return [name for name in ROLE_PERMISSIONS[role] if not granted(name)]
