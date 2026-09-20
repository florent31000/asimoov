"""Shared stubs and fakes for the perception tests.

Imported by name from the test modules: `conftest.py` puts this directory on
`sys.path`, because pytest's importlib import mode does not.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import numpy as np
import websockets

from asimoov.contracts.envelope import Envelope
from asimoov.perception.camera.base import CameraSource, Frame


class deadline:
    """Async context manager equivalent to ``asyncio.timeout`` on Python 3.10.

    ``asyncio.timeout`` only exists from 3.11; the CI matrix runs 3.10 too.
    """

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._expired = False
        self._handle: asyncio.TimerHandle | None = None

    def _expire(self, task: asyncio.Task[Any]) -> None:
        self._expired = True
        task.cancel()

    async def __aenter__(self) -> deadline:
        task = asyncio.current_task()
        assert task is not None
        loop = asyncio.get_running_loop()
        self._handle = loop.call_later(self._seconds, self._expire, task)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._handle is not None:
            self._handle.cancel()
        if self._expired and exc_type is asyncio.CancelledError:
            raise TimeoutError(f"deadline of {self._seconds}s expired") from None
        return False


def synthetic_image(width: int = 640, height: int = 360, *, seed: int = 0) -> np.ndarray:
    """A deterministic BGR image with a few drawn rectangles (never a face)."""
    rng = np.random.default_rng(seed)
    image = np.full((height, width, 3), 32, dtype=np.uint8)
    for _ in range(6):
        x0 = int(rng.integers(0, width - 40))
        y0 = int(rng.integers(0, height - 40))
        image[y0 : y0 + 40, x0 : x0 + 40] = rng.integers(0, 255, size=3, dtype=np.uint8)
    return image


@dataclass(frozen=True)
class StubDetection:
    bbox: tuple[float, float, float, float]
    score: float
    kps: Any = None


class StubDetector:
    """Replays a scripted list of detections, one entry per frame."""

    def __init__(self, script: list[list[StubDetection]]) -> None:
        self.script = script
        self.calls = 0

    def detect(self, image: np.ndarray) -> list[StubDetection]:
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return self.script[index]


class StubEmbedder:
    """Returns a fixed unit vector per detection position, no ONNX involved."""

    dim = 8

    def __init__(self, vector: np.ndarray | None = None) -> None:
        base = np.zeros(self.dim, dtype=np.float32) if vector is None else np.asarray(vector)
        if vector is None:
            base[0] = 1.0
        self.vector = base.astype(np.float32)
        self.calls = 0

    def embed_aligned(self, aligned: np.ndarray) -> np.ndarray:
        self.calls += 1
        return self.vector


class ScriptedCamera(CameraSource):
    """Yields a fixed number of synthetic frames, then ends the stream.

    Frame timestamps advance by ``period`` (5 fps by default) so tracker
    hysteresis and the embedding cadence are exercised in virtual time.
    """

    def __init__(
        self, frames: int = 3, *, width: int = 640, height: int = 360, period: float = 0.2
    ) -> None:
        self.total = frames
        self.period = period
        self.width = width
        self.height = height
        self.seq = 0
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def read(self) -> Frame | None:
        if self.seq >= self.total:
            return None
        self.seq += 1
        return Frame(
            bgr=synthetic_image(self.width, self.height, seed=self.seq),
            ts=self.seq * self.period,
            seq=self.seq,
        )


class RecordingBus:
    """A `contracts.bus.Bus` that keeps every envelope in memory."""

    def __init__(self, src: str = "perception.face_id") -> None:
        self.src = src
        self.envelopes: list[Envelope] = []

    async def publish(self, topic, data, *, kind="percept", corr=None) -> None:
        self.envelopes.append(
            Envelope(kind=kind, topic=topic, src=self.src, data=dict(data), corr=corr)
        )

    def subscribe(self, pattern, handler):  # pragma: no cover - unused by these tests
        raise NotImplementedError

    async def request(self, topic, data, *, timeout_s):  # pragma: no cover - unused
        raise NotImplementedError

    def latest(self, topic):  # pragma: no cover - unused
        return None

    def topics(self) -> list[str]:
        return [envelope.topic for envelope in self.envelopes]

    def of_topic(self, topic: str) -> list[Envelope]:
        return [envelope for envelope in self.envelopes if envelope.topic == topic]


class StubStore:
    """The slice of `MemoryStore` the module uses, in memory."""

    def __init__(self, match: tuple[str, float] | None = None) -> None:
        self.match = match
        self.embeddings: list[tuple[str, str, Any, float]] = []
        self.persons: dict[str, Any] = {}
        self.reloads = 0

    async def match_face(self, vec):
        return self.match

    async def reload_gallery(self) -> int:
        self.reloads += 1
        return len({person_id for person_id, *_ in self.embeddings})

    async def get_person(self, person_id: str):
        return self.persons.get(person_id)

    async def upsert_person(self, person):
        self.persons[person.id] = person
        return person

    async def add_face_embedding(self, person_id, model, vec, quality) -> None:
        self.embeddings.append((person_id, model, vec, quality))


class FakeHub:
    """A WebSocket server speaking WS1's hub protocol.

    Answers the `hello` control message with a `welcome`, records every other
    text message in `received`, and can push envelopes or raw binary frames
    back to the last client.
    """

    def __init__(self, *, welcome: bool = True) -> None:
        self.welcome = welcome
        self.hello: dict | None = None
        self.patterns: tuple[str, ...] = ()
        self.received: list[dict] = []
        self.connections: list = []
        self.paths: list[str] = []
        self._server = None

    async def __aenter__(self) -> FakeHub:
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def _handle(self, connection) -> None:
        self.connections.append(connection)
        self.paths.append(getattr(connection.request, "path", ""))
        try:
            self.hello = json.loads(await connection.recv())
            self.patterns = tuple(self.hello.get("subscribe") or ())
            if not self.welcome:
                await asyncio.sleep(5.0)
                return
            await connection.send(json.dumps({"type": "welcome", "v": 1}))
            async for message in connection:
                if isinstance(message, bytes):
                    continue
                payload = json.loads(message)
                if payload.get("type") == "subscribe":
                    self.patterns = tuple(payload.get("patterns") or ())
                    continue
                self.received.append(payload)
        except websockets.ConnectionClosed:
            pass

    async def push(self, envelope: Envelope) -> None:
        await self.connections[-1].send(json.dumps(envelope.to_dict()))

    async def push_bytes(self, payload: bytes) -> None:
        await self.connections[-1].send(payload)

    async def wait_for(self, count: int, timeout: float = 2.0) -> None:
        async with deadline(timeout):
            while len(self.received) < count:
                await asyncio.sleep(0.01)

    async def wait_for_patterns(self, patterns: tuple[str, ...], timeout: float = 2.0) -> None:
        async with deadline(timeout):
            while self.patterns != patterns:
                await asyncio.sleep(0.01)
