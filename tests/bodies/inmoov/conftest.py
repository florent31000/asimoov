from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

# pytest runs with --import-mode=importlib, which does not put the test
# directory on sys.path: do it here so fake_firmware imports everywhere.
sys.path.insert(0, str(Path(__file__).parent))

from fake_firmware import (  # noqa: E402
    BENCH_CHANNELS,
    BUST_CHANNELS,
    FakeFirmware,
    FakeSerialPort,
    serve_tcp,
)

from asimoov.bodies.inmoov.link import SerialLink, TcpLink  # noqa: E402


@pytest.fixture
def bench_firmware() -> FakeFirmware:
    return FakeFirmware(channels=BENCH_CHANNELS)


@pytest.fixture
def bust_firmware() -> FakeFirmware:
    return FakeFirmware(channels=BUST_CHANNELS)


@pytest.fixture
async def tcp_link(bench_firmware: FakeFirmware) -> AsyncIterator[tuple[TcpLink, FakeFirmware]]:
    async for pair in _tcp_link(bench_firmware):
        yield pair


@pytest.fixture
async def bust_tcp_link(bust_firmware: FakeFirmware) -> AsyncIterator[tuple[TcpLink, FakeFirmware]]:
    async for pair in _tcp_link(bust_firmware):
        yield pair


async def _tcp_link(firmware: FakeFirmware) -> AsyncIterator[tuple[TcpLink, FakeFirmware]]:
    server = await serve_tcp(firmware)
    port = server.sockets[0].getsockname()[1]
    link = TcpLink("127.0.0.1", port)
    await link.start()
    await _wait_connected(link)
    yield link, firmware
    await link.close()
    server.close()
    await server.wait_closed()


@pytest.fixture
async def serial_link(
    bench_firmware: FakeFirmware,
) -> AsyncIterator[tuple[SerialLink, FakeFirmware]]:
    link = SerialLink("fake", serial_factory=lambda: FakeSerialPort(bench_firmware))
    await link.start()
    await _wait_connected(link)
    yield link, bench_firmware
    await link.close()


async def _wait_connected(link: TcpLink | SerialLink, timeout_s: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not link.connected:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("link never came up")
        await asyncio.sleep(0.01)
