"""Framing, keepalive, priority writes and reconnection of the link."""

from __future__ import annotations

import asyncio
import time

import pytest
from fake_firmware import BUST_CHANNELS, FakeFirmware, FakeSerialPort, make_tcp_body

from asimoov.bodies.inmoov import link as link_module
from asimoov.bodies.inmoov.link import SerialLink
from asimoov.contracts.body import BodyContext


async def test_reply_framing_over_tcp(tcp_link) -> None:
    link, _firmware = tcp_link
    reply = await link.send("V")
    assert reply.status == "END"
    assert reply.lines[0].startswith("V asimoov-inmoov")
    assert any(line.startswith("CH 100 finger_demo") for line in reply.lines)

    assert (await link.send("K")).status == "OK"

    error = await link.send("S 42 10")
    assert error.status == "ERR"
    assert error.error == "unknown channel"
    assert not error.ok


async def test_reply_framing_over_serial(serial_link) -> None:
    link, firmware = serial_link
    assert (await link.send("E 100")).ok
    assert firmware.attached(100)
    assert (await link.send("?")).status == "END"


async def test_keepalive_is_sent_every_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(link_module, "KEEPALIVE_INTERVAL_S", 0.05)
    firmware = FakeFirmware()
    body, server = await make_tcp_body(firmware)
    try:
        await body.start(BodyContext())
        await asyncio.sleep(0.25)
        assert firmware.commands.count("K") >= 3
    finally:
        await body.stop()
        server.close()
        await server.wait_closed()


async def test_timeout_does_not_fabricate_a_reply(tcp_link, monkeypatch) -> None:
    link, firmware = tcp_link
    monkeypatch.setattr(firmware, "handle_line", lambda line: [])
    reply = await link.send("K", timeout_s=0.05)
    assert reply.status == ""
    assert not reply.ok
    assert "no reply" in reply.error


async def test_priority_write_does_not_wait_for_the_command_in_flight(tcp_link) -> None:
    link, firmware = tcp_link
    slow = asyncio.create_task(link.send("?"))
    await link.send_priority("!")
    assert (await slow).status == "END"
    await _wait(lambda: firmware.estopped, 1.0)

    # The stray `OK` of `!` must not be paired with the next command.
    await asyncio.sleep(0.05)
    assert (await link.send("E 100")).status == "OK"
    assert (await link.send("V")).status == "END"


async def test_disconnect_stops_the_body_and_reconnect_reapplies_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(link_module, "RECONNECT_DELAY_S", 0.05)
    firmware = FakeFirmware()
    body, server = await make_tcp_body(firmware, limits={"finger_demo": [10, 60]})
    try:
        await body.start(BodyContext())
        await _wait(lambda: "L 100 10 60" in firmware.commands, 2.0)
        assert (await body.health()).connected

        firmware.commands.clear()
        stopped: list[str] = []
        original_stop_all = body.stop_all

        async def record(reason: str) -> None:
            stopped.append(reason)
            await original_stop_all(reason)

        body.stop_all = record
        await body.simulate_disconnect()
        await _wait(lambda: bool(stopped), 2.0)
        assert not (await body.health()).connected

        # The board rebooted with its config.h defaults: tighten them again.
        await _wait(lambda: "L 100 10 60" in firmware.commands, 3.0)
        await _wait_healthy(body, 2.0)
    finally:
        await body.stop()
        server.close()
        await server.wait_closed()


async def _wait_healthy(body, timeout_s: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not (await body.health()).connected:
        if loop.time() > deadline:
            raise AssertionError("the body never reported itself connected again")
        await asyncio.sleep(0.02)


async def _wait(predicate, timeout_s: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.02)


async def test_an_estopped_link_rearms_before_the_next_move(tcp_link) -> None:
    link, firmware = tcp_link
    assert (await link.send("E 100")).ok
    await link.send_priority("!")
    await _wait(lambda: firmware.estopped, 1.0)

    reply = await link.send("M 100:50 T100")
    assert reply.ok, reply.error
    assert "E all" in firmware.commands
    assert not firmware.estopped
    assert link.estopped is False


async def test_health_reports_the_estop_while_the_body_is_stopped() -> None:
    firmware = FakeFirmware(channels=BUST_CHANNELS)
    body, server = await make_tcp_body(firmware)
    try:
        await body.start(BodyContext())
        assert "ERR estop" not in (await body.health()).errors
        await body.stop_all("test")
        await _wait(lambda: firmware.estopped, 1.0)
        assert "ERR estop" in (await body.health()).errors
    finally:
        await body.stop()
        server.close()
        await server.wait_closed()


class _SlowPort(FakeSerialPort):
    """A port whose write blocks the calling thread for 50 ms."""

    def write(self, payload: bytes) -> int:
        time.sleep(0.05)
        return super().write(payload)


async def test_the_serial_write_does_not_block_the_event_loop(
    bench_firmware: FakeFirmware,
) -> None:
    link = SerialLink("fake", serial_factory=lambda: _SlowPort(bench_firmware))
    await link.start()
    await _wait(lambda: link.connected, 2.0)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        assert (await link.send("K", timeout_s=1.0)).ok
        assert ticks >= 3, f"the event loop only ran {ticks} times during the write"
    finally:
        task.cancel()
        await link.close()
