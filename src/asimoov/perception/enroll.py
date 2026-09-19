"""`python -m asimoov.perception.enroll --name Sam --camera opencv:0`.

The bench equivalent of `asimoov enroll --name Sam` (WS1's CLI calls
`enroll_person` directly). Runs the face_id pipeline locally, with no hub:
it opens the camera, waits for a single stable face, collects 5 embeddings of
quality > 0.6, and writes them to the SQLite store. Nothing is published and
no image is saved.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import time
from typing import Any

from asimoov.perception import PerceptionError
from asimoov.perception.camera import open_camera
from asimoov.perception.face_id.enroll import DEFAULT_SAMPLES, DEFAULT_TIMEOUT_S
from asimoov.perception.face_id.module import FaceIdModule
from asimoov.perception.store import SqliteFaceStore

log = logging.getLogger("asimoov.perception.enroll")

DEFAULT_CAMERA = "opencv:0"
#: How long to wait for exactly one face before giving up.
FACE_WAIT_S = 15.0


def person_id_for(name: str) -> str:
    """`Sam` -> `p_sam`, matching the ids the mind's `remember_person` creates."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    if not slug:
        raise PerceptionError(f"cannot derive a person id from name {name!r}")
    return f"p_{slug}"


async def enroll_person(
    name: str,
    *,
    camera_spec: str = DEFAULT_CAMERA,
    db: str | None = None,
    person_id: str | None = None,
    samples: int = DEFAULT_SAMPLES,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Enrol `name` from a local camera. The hook WS1's `asimoov enroll` calls.

    Raises:
        PerceptionError: if no single face shows up in time.
    """
    from asimoov.perception.face_id.detector_scrfd import ScrfdDetector
    from asimoov.perception.face_id.embedder_arcface import ArcFaceEmbedder

    store = SqliteFaceStore(db)
    module = FaceIdModule(
        open_camera(camera_spec),
        store=store,
        detector=ScrfdDetector(),
        embedder=ArcFaceEmbedder(),
        enroll_samples=samples,
        enroll_timeout_s=timeout_s,
    )
    identifier = person_id or person_id_for(name)
    await module.start(_headless_context())
    try:
        track_id = await _wait_for_single_face(module)
        return await module.handle_command(
            "face_id.enroll", {"track_id": track_id, "person_id": identifier, "name": name}
        )
    finally:
        await module.stop()
        store.close()


def _headless_context():
    from asimoov.contracts.perception import PerceptionContext

    return PerceptionContext(config={})


async def _wait_for_single_face(module: FaceIdModule) -> str:
    deadline = time.monotonic() + FACE_WAIT_S
    while time.monotonic() < deadline:
        tracks = list(module.tracker.tracks)
        if len(tracks) == 1:
            return tracks[0]
        if len(tracks) > 1:
            raise PerceptionError("several faces in frame: enrol one person at a time")
        await asyncio.sleep(0.1)
    raise PerceptionError(f"no face detected within {FACE_WAIT_S:.0f}s")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m asimoov.perception.enroll")
    parser.add_argument("--name", required=True, help="person's name, e.g. Sam")
    parser.add_argument("--camera", default=DEFAULT_CAMERA, help="opencv:N")
    parser.add_argument("--db", help="SQLite memory database (default $ASIMOOV_HOME/memory.db)")
    parser.add_argument("--person-id", help="explicit person id (default p_<name>)")
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s: %(message)s")
    try:
        payload = asyncio.run(
            enroll_person(
                args.name,
                camera_spec=args.camera,
                db=args.db,
                person_id=args.person_id,
                samples=args.samples,
                timeout_s=args.timeout,
            )
        )
    except PerceptionError as exc:
        print(f"enroll: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    if payload.get("ok"):
        print(f"enrolled {payload['person_id']} with {payload['samples']} samples")
        return 0
    print(f"enroll failed: {payload.get('reason')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
