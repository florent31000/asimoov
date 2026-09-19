"""Desktop/Android Kivy app showing the face: ``python -m asimoov.faces.kivy.app --demo``.

The Android entrypoint (WS7's ``android/main.py``) builds the same app and
feeds `KivyFace.render` from the runtime instead of the demo sequence.
"""

from __future__ import annotations

import argparse
import asyncio
import threading
import time
from typing import Any

from asimoov.faces.kivy.renderer import KivyFace, require_kivy
from asimoov.faces.server import demo_state


def build_app(face: KivyFace) -> Any:
    """Return a Kivy `App` whose root widget is ``face.widget``."""
    require_kivy()
    from kivy.app import App

    class FaceApp(App):  # type: ignore[misc, valid-type]
        title = "ASIMOOV"

        def build(self) -> Any:
            return face.widget

        def on_start(self) -> None:
            asyncio.run(face.start({}))

        def on_stop(self) -> None:
            asyncio.run(face.stop())

    return FaceApp()


def _drive_demo(face: KivyFace, stop: threading.Event) -> None:
    started_at = time.monotonic()
    loop = asyncio.new_event_loop()
    try:
        while not stop.is_set():
            loop.run_until_complete(face.render(demo_state(time.monotonic() - started_at)))
            stop.wait(1.0 / 30)
    finally:
        loop.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m asimoov.faces.kivy.app")
    parser.add_argument("--theme", default="dark", choices=("dark", "light"))
    parser.add_argument("--demo", action="store_true", help="play a scripted FaceState sequence")
    args = parser.parse_args()

    face = KivyFace(theme=args.theme)
    app = build_app(face)
    stop = threading.Event()
    if args.demo:
        threading.Thread(target=_drive_demo, args=(face, stop), daemon=True).start()
    try:
        app.run()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
