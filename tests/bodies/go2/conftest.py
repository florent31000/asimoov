"""Go2 fixtures. The doubles themselves live in `fakes.py`, next to this file."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# pytest runs with --import-mode=importlib, which does not put the test
# directory on sys.path: do it here so `fakes` imports everywhere.
sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeConnection, FakePubSub  # noqa: E402

from asimoov.bodies.go2.adapter import Go2Body  # noqa: E402


class RecordingBus:
    """Minimal `contracts.bus.Bus` recorder."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any], str]] = []

    async def publish(self, topic, data, *, kind="percept", corr=None):  # noqa: ANN001
        self.published.append((topic, dict(data), kind))

    def subscribe(self, pattern, handler):  # noqa: ANN001
        raise NotImplementedError

    async def request(self, topic, data, *, timeout_s):  # noqa: ANN001
        raise NotImplementedError

    def latest(self, topic):  # noqa: ANN001
        return None


@pytest.fixture
def bus() -> RecordingBus:
    return RecordingBus()


@pytest.fixture
def pub_sub() -> FakePubSub:
    return FakePubSub()


@pytest.fixture
def connection() -> FakeConnection:
    return FakeConnection()


@pytest.fixture
def body(connection: FakeConnection) -> Go2Body:
    async def connect(config: dict[str, Any]) -> FakeConnection:
        connection.isConnected = True
        connection.disconnected = False
        return connection

    return Go2Body({}, connect=connect)
