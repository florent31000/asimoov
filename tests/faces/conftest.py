"""Shared doubles for the WS6 tests: a face page connection and a bus."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

_EOF = object()


class FakePage:
    """A face page connection: records what the server sends, feeds messages."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: tuple[int, str] | None = None
        self._inbox: asyncio.Queue[Any] = asyncio.Queue()

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)
        self._inbox.put_nowait(_EOF)

    def feed(self, message: str | bytes) -> None:
        self._inbox.put_nowait(message)

    def end(self) -> None:
        self._inbox.put_nowait(_EOF)

    async def __aiter__(self) -> AsyncIterator[str | bytes]:
        while True:
            message = await self._inbox.get()
            if message is _EOF:
                return
            yield message


class RecordingBus:
    """Minimal `contracts.bus.Bus` recording every publish."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any], str]] = []

    async def publish(
        self, topic: str, data: Any, *, kind: str = "percept", corr: str | None = None
    ) -> None:
        self.published.append((topic, dict(data), kind))

    def subscribe(self, pattern: str, handler: Any) -> Any:
        raise NotImplementedError

    async def request(self, topic: str, data: Any, *, timeout_s: float) -> Any:
        raise NotImplementedError

    def latest(self, topic: str) -> Any:
        return None


@pytest.fixture
def page() -> FakePage:
    return FakePage()


@pytest.fixture
def bus() -> RecordingBus:
    return RecordingBus()
