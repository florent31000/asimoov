"""Standalone face page server: ``python -m asimoov.faces.web --demo``.

Serves http://host:port/face without a hub and, with ``--demo``, drives it
with the scripted `FaceState` sequence of `faces.server.demo_state`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import time

from asimoov.faces.server import DEFAULT_PORT, WebFace, demo_state, serve_face

DEMO_HZ = 30.0


async def _drive_demo(face: WebFace) -> None:
    started_at = time.monotonic()
    while True:
        await face.render(demo_state(time.monotonic() - started_at))
        await asyncio.sleep(1.0 / DEMO_HZ)


async def _run(args: argparse.Namespace) -> None:
    face = WebFace()
    server = await serve_face(face, host=args.host, port=args.port, token=args.token)
    url = f"http://{args.host}:{args.port}/face"
    if args.token:
        url += f"?token={args.token}"
    logging.getLogger(__name__).info("face page on %s", url)
    print(f"face page on {url}")  # noqa: T201 -- this is a CLI
    demo = asyncio.create_task(_drive_demo(face)) if args.demo else None
    try:
        await server.wait_closed()
    finally:
        if demo is not None:
            demo.cancel()
        await face.stop()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m asimoov.faces.web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", default=None)
    parser.add_argument("--demo", action="store_true", help="emit a scripted FaceState sequence")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(args))


if __name__ == "__main__":
    main()
