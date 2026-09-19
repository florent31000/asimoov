"""APK entry point.

Kivy owns the main thread; the runtime (or the hub client, in `head` role)
owns a background thread with its own asyncio loop. Nothing here knows an API
key: the settings screen writes `getFilesDir()/secrets.yaml` and this reads
it.

Roles (plan.md 4.10), same APK:
  core       everything on the phone: runtime, voice, face, mic and speaker.
  head       renderer + microphone/speaker/camera attached to the hub of a
             core running elsewhere.
  face_only  not this APK -- a web page in Chrome, no install.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    # build.sh stages `src/asimoov` next to this file.
    sys.path.insert(0, str(HERE))

from asimoov.android import foreground, permissions  # noqa: E402
from asimoov.android import settings as app_settings  # noqa: E402
from asimoov.contracts.face import FaceGaze, FaceState  # noqa: E402
from asimoov.contracts.vocab import TOPICS  # noqa: E402
from asimoov.core.config import load_robot_config  # noqa: E402
from asimoov.core.runtime import Runtime  # noqa: E402
from asimoov.faces.kivy.app import build_app  # noqa: E402
from asimoov.faces.kivy.renderer import KivyFace  # noqa: E402

log = logging.getLogger("asimoov.android")

ROBOT_DIR = HERE / "robots" / "avatar"


def _run_core(face: KivyFace, settings: app_settings.Settings, stop: threading.Event) -> None:
    """Whole robot on the phone."""
    if settings.api_key:
        os.environ.setdefault("OPENAI_API_KEY", settings.api_key)

    async def go() -> None:
        config = load_robot_config(ROBOT_DIR)
        runtime = Runtime.build(config, face_names=())
        runtime.faces = (face,)
        await runtime.start()
        try:
            while not stop.is_set():
                await asyncio.sleep(0.2)
        finally:
            await runtime.stop()

    asyncio.run(go())


def _run_head(face: KivyFace, settings: app_settings.Settings, stop: threading.Event) -> None:
    """Renderer only: follow the `face.state` of a core running elsewhere."""
    from asimoov.core.bus.client import BusClient

    address = settings.body_address
    if not address:
        raise RuntimeError("role 'head' needs the address of the core in the settings screen")
    url = address if address.startswith("ws") else f"ws://{address}:7331/bus"

    async def go() -> None:
        client = BusClient(url, settings.api_key, f"face-{settings.name or 'phone'}",
                           subscriptions=(TOPICS.FACE_STATE,))

        async def on_state(envelope) -> None:
            data = envelope.data
            gaze = data.get("gaze") or {}
            await face.render(
                FaceState(
                    emotion=data["emotion"],
                    intensity=data.get("intensity", 1.0),
                    gaze=FaceGaze(gaze.get("x", 0.0), gaze.get("y", 0.0)),
                    lip=data.get("lip", 0.0),
                    blink=data.get("blink", False),
                    eyelids=data.get("eyelids", 0.0),
                    talking=data.get("talking", False),
                )
            )

        client.subscribe(TOPICS.FACE_STATE, on_state)
        await client.connect()
        try:
            while not stop.is_set():
                await asyncio.sleep(0.2)
        finally:
            await client.close()

    asyncio.run(go())


WORKERS = {"core": _run_core, "head": _run_head}


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = app_settings.load()

    if settings.role == "face_only":
        raise SystemExit(
            "role 'face_only' does not use this app: open http://<core>:7331/face in Chrome"
        )
    if settings.role not in WORKERS:
        raise SystemExit(f"unknown role {settings.role!r} in {app_settings.settings_path()}")
    if not settings.is_complete():
        raise SystemExit(
            f"incomplete settings in {app_settings.settings_path()}: "
            "the settings screen must write api_key (core) or body_address (head)"
        )

    permissions.request_for_role(settings.role)

    face = KivyFace(theme="dark")
    app = build_app(face)
    stop = threading.Event()
    worker = threading.Thread(
        target=WORKERS[settings.role], args=(face, settings, stop), daemon=True
    )

    if settings.role == "core":
        foreground.acquire_wake_lock()

    worker.start()
    try:
        app.run()
    finally:
        stop.set()
        worker.join(timeout=5)
        foreground.release_wake_lock()


if __name__ == "__main__":
    main()
