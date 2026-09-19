"""Keeping the core alive with the screen off.

Two separate things, both optional and both off by default:

- a partial wake lock, so the CPU keeps running the audio loop;
- a python-for-android foreground service, so Android does not kill the
  process. The service only exists if the APK was built with a `services =`
  line in `buildozer.spec` (see `android/README.md`); asking for it in an APK
  built without one raises instead of pretending to start.

Import-guarded: importable and callable on a laptop, where everything is a
logged no-op returning False.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

try:  # pragma: no cover - only importable inside the APK
    from jnius import autoclass

    ON_ANDROID = True
except ImportError:  # pragma: no cover - the normal case off-device
    autoclass = None
    ON_ANDROID = False

_wake_lock = None


def is_android() -> bool:
    return ON_ANDROID


def _activity():
    return autoclass("org.kivy.android.PythonActivity").mActivity


def acquire_wake_lock(tag: str = "asimoov:core") -> bool:
    """Hold a partial wake lock. Idempotent."""
    global _wake_lock
    if not ON_ANDROID:
        log.warning("not running on Android: no wake lock")
        return False
    if _wake_lock is not None:
        return True

    context = _activity()
    power_service = autoclass("android.content.Context").POWER_SERVICE
    manager = context.getSystemService(power_service)
    partial = autoclass("android.os.PowerManager").PARTIAL_WAKE_LOCK
    _wake_lock = manager.newWakeLock(partial, tag)
    _wake_lock.acquire()
    log.info("wake lock acquired")
    return True


def release_wake_lock() -> bool:
    global _wake_lock
    if _wake_lock is None:
        return False
    _wake_lock.release()
    _wake_lock = None
    log.info("wake lock released")
    return True


def _service_class(name: str):
    package = _activity().getPackageName()
    class_name = f"{package}.Service{name.capitalize()}"
    try:
        return autoclass(class_name)
    except Exception as exc:  # jnius raises JavaException
        raise RuntimeError(
            f"{class_name} does not exist: this APK was built without "
            f"`services = {name}:service.py:foreground` in buildozer.spec"
        ) from exc


def start_service(name: str = "core", argument: str = "") -> bool:
    """Start the foreground service declared under `name`."""
    if not ON_ANDROID:
        log.warning("not running on Android: no foreground service")
        return False
    _service_class(name).start(_activity(), argument)
    log.info("foreground service %s started", name)
    return True


def stop_service(name: str = "core") -> bool:
    if not ON_ANDROID:
        return False
    _service_class(name).stop(_activity())
    log.info("foreground service %s stopped", name)
    return True
