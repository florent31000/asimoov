"""`python -m asimoov.perception --hub ws://host:7331 --config robot.yaml`.

A separate OS process: it connects to the core's hub as a bus client, runs the
enabled perception modules, and answers `cmd perception.<module>.<name>`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from dataclasses import replace
from typing import Any

from asimoov.contracts.envelope import Envelope
from asimoov.contracts.perception import PerceptionContext, PerceptionModule
from asimoov.core.bus.client import BusClient, read_token
from asimoov.perception import PerceptionError
from asimoov.perception.camera import open_camera
from asimoov.perception.config import PerceptionConfig, load_config
from asimoov.perception.face_id.module import FaceIdModule
from asimoov.perception.store import SqliteFaceStore
from asimoov.perception.vad_module import VadModule

log = logging.getLogger("asimoov.perception")

COMMAND_PREFIX = "perception."
COMMAND_PATTERN = "perception.*"
HEALTH_TOPIC = "perception.health"
MODULE_ID = "perception"
FACE_ID_SRC = "perception.face_id"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m asimoov.perception")
    parser.add_argument("--hub", help="hub URL, e.g. ws://192.168.1.10:7331 (overrides config)")
    parser.add_argument("--config", help="path to robot.yaml")
    parser.add_argument("--camera", help="camera spec, overrides perception.face_id.camera")
    parser.add_argument("--db", help="SQLite memory database, overrides perception.face_id.db")
    parser.add_argument("--module-id", default=MODULE_ID, help="bus identity of this process")
    parser.add_argument("--log-level", default="INFO")
    return parser


class Process:
    """Owns the hub connection and the enabled modules."""

    def __init__(self, config: PerceptionConfig, *, hub: BusClient) -> None:
        self.config = config
        self.hub = hub
        self.modules: dict[str, PerceptionModule] = {}
        self.errors: list[str] = []
        self._tasks: set[asyncio.Task[None]] = set()

    def health(self) -> dict[str, Any]:
        """Snapshot published on `perception.health`."""
        return {
            "module_id": self.hub.module_id,
            "modules": sorted(self.modules),
            "errors": list(self.errors),
        }

    async def _record_error(self, message: str) -> None:
        """Keep a failure visible on the bus instead of dropping it."""
        self.errors.append(message)
        await self.hub.publish(HEALTH_TOPIC, self.health(), kind="state")

    async def start(self) -> None:
        self.hub.subscribe(COMMAND_PATTERN, self._on_command)
        try:
            await self.hub.connect()
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise PerceptionError(f"no hub answering at {self.hub.url}") from exc
        if self.config.face_id.enabled:
            self.modules["face_id"] = self._build_face_id()
        if self.config.vad_enabled:
            self.modules["vad"] = VadModule()
        if not self.modules:
            raise PerceptionError("no perception module enabled in robot.yaml")
        for name, module in self.modules.items():
            await module.start(self._context(f"{COMMAND_PREFIX}{name}"))
            log.info("started module %s", name)

    async def stop(self) -> None:
        for name, module in self.modules.items():
            try:
                await module.stop()
            except Exception as exc:  # noqa: BLE001 - the other modules must still stop
                log.exception("module %s failed to stop", name)
                await self._record_error(f"{name}.stop: {type(exc).__name__}: {exc}")
        for task in list(self._tasks):
            task.cancel()
        await self.hub.close()

    def _build_face_id(self) -> FaceIdModule:
        from asimoov.perception.face_id.detector_scrfd import ScrfdDetector
        from asimoov.perception.face_id.embedder_arcface import ArcFaceEmbedder

        return FaceIdModule(
            open_camera(self.config.face_id.camera, hub=self.hub),
            store=SqliteFaceStore(self.config.face_id.db),
            detector=ScrfdDetector(),
            embedder=ArcFaceEmbedder(),
            fov_h_deg=self.config.face_id.fov_h_deg,
        )

    def _context(self, src: str) -> PerceptionContext:
        bus = ScopedBus(self.hub, src)
        return PerceptionContext(
            config={},
            publish=bus.publish,
            subscribe=bus.subscribe,
            request=bus.request,
            bus=bus,
        )

    async def _on_command(self, envelope: Envelope) -> None:
        if envelope.kind != "cmd":
            return
        task = asyncio.create_task(self._run_command(envelope))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_command(self, envelope: Envelope) -> None:
        name = envelope.topic[len(COMMAND_PREFIX) :]
        for module in self.modules.values():
            try:
                payload = await module.handle_command(name, dict(envelope.data))
            except KeyError:
                continue
            except Exception as exc:  # noqa: BLE001 - a failed command must still reply
                log.exception("command %s failed", envelope.topic)
                payload = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
            await self.hub.reply(envelope, payload)
            return
        await self.hub.reply(envelope, {"ok": False, "reason": f"unknown command {name}"})


class ScopedBus:
    """A `contracts.bus.Bus` view of the hub with a fixed envelope `src`.

    One process hosts several producers (`perception.face_id`,
    `perception.vad`) but holds one socket; the envelope's `src` must still
    name the producer.
    """

    def __init__(self, hub: BusClient, src: str) -> None:
        self._hub = hub
        self.src = src

    async def publish(self, topic, data, *, kind="percept", corr=None) -> None:
        await self._hub.send(
            Envelope(kind=kind, topic=topic, src=self.src, data=dict(data), corr=corr)
        )

    def subscribe(self, pattern, handler):
        return self._hub.subscribe(pattern, handler)

    async def request(self, topic, data, *, timeout_s):
        return await self._hub.request(topic, data, timeout_s=timeout_s)

    def latest(self, topic):
        return self._hub.latest(topic)


async def run(args: argparse.Namespace) -> int:
    config = load_config(args.config) if args.config else PerceptionConfig()
    face_id = config.face_id
    if args.camera:
        face_id = replace(face_id, enabled=True, camera=args.camera)
    if args.db:
        face_id = replace(face_id, db=args.db)
    config = replace(config, face_id=face_id)

    hub = BusClient(
        args.hub or config.hub_url,
        read_token(),
        args.module_id,
        subscriptions=(),
    )
    process = Process(config, hub=hub)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is not None:
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signum, stop.set)
    await process.start()
    try:
        await stop.wait()
    finally:
        await process.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0
    except PerceptionError as exc:
        print(f"asimoov.perception: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
