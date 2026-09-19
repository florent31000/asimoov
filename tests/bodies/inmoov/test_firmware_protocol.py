"""The firmware side of the protocol, through its Python twin."""

from __future__ import annotations

import asyncio

from fake_firmware import (
    BUST_CHANNELS,
    DETACH_AFTER_S,
    HOLD_AFTER_S,
    LINE_MAX,
    FakeFirmware,
    serve_tcp,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def bench(clock: Clock | None = None) -> FakeFirmware:
    return FakeFirmware(clock=clock or Clock())


def test_version_lists_the_channels() -> None:
    firmware = FakeFirmware(channels=BUST_CHANNELS)
    lines = firmware.handle_line("V")
    assert lines[0] == "V asimoov-inmoov 1.0.0 1 uno_r4_wifi 8"
    assert "CH 8 neck_yaw 30 150 90" in lines
    assert lines[-1] == "END"


def test_nothing_is_attached_at_boot() -> None:
    firmware = bench()
    state = firmware.handle_line("?")
    assert state[0].split()[-2] == "0"
    assert not firmware.any_attached()


def test_hard_limits_clamp_every_target() -> None:
    firmware = bench()
    assert firmware.handle_line("E 100") == ["OK"]
    assert firmware.handle_line("S 100 500") == ["OK"]
    assert firmware.position(100) == 108
    assert firmware.handle_line("S 100 -40") == ["OK"]
    assert firmware.position(100) == 2


def test_limits_can_only_tighten() -> None:
    firmware = bench()
    firmware.handle_line("L 100 20 60")
    assert firmware.handle_line("V")[1] == "CH 100 finger_demo 20 60 2"
    # Asking for a wider range keeps the hard stops of config.h.
    firmware.handle_line("L 100 0 180")
    assert firmware.handle_line("V")[1] == "CH 100 finger_demo 2 108 2"


def test_declare_does_not_move() -> None:
    firmware = bench()
    firmware.handle_line("E 100")
    assert firmware.handle_line("P 100 40") == ["OK"]
    assert firmware.position(100) == 40
    assert firmware.handle_line("?")[0].endswith(" 1 0")


def test_move_interpolates_without_blocking() -> None:
    clock = Clock()
    firmware = bench(clock)
    firmware.handle_line("E 100")
    firmware.handle_line("P 100 0")
    assert firmware.handle_line("M 100:100 T1000") == ["OK"]
    clock.advance(0.5)
    half = firmware.position(100)
    assert 45 < half < 55  # ease-in-out is symmetric at mid-course
    clock.advance(0.6)
    assert firmware.position(100) == 100


def test_estop_freezes_then_refuses_motion() -> None:
    clock = Clock()
    firmware = bench(clock)
    firmware.handle_line("E 100")
    firmware.handle_line("M 100:100 T1000")
    clock.advance(0.2)
    assert firmware.handle_line("!") == ["OK"]
    frozen = firmware.position(100)
    clock.advance(0.5)
    assert firmware.position(100) == frozen
    assert firmware.handle_line("M 100:50 T200") == ["ERR estop"]
    assert firmware.handle_line("S 100 50") == ["ERR estop"]

    clock.advance(0.4)
    firmware.tick()
    assert not firmware.any_attached()

    # `E` is how a runtime re-arms after an emergency stop.
    assert firmware.handle_line("E 100") == ["OK"]
    assert firmware.handle_line("S 100 50") == ["OK"]


def test_watchdog_holds_then_detaches() -> None:
    clock = Clock()
    firmware = bench(clock)
    firmware.handle_line("E 100")
    firmware.handle_line("M 100:100 T5000")

    clock.advance(HOLD_AFTER_S + 0.1)
    firmware.tick()
    assert firmware.holding
    assert firmware.any_attached()
    held = firmware.position(100)
    clock.advance(0.5)
    assert firmware.position(100) == held

    clock.advance(DETACH_AFTER_S)
    firmware.tick()
    assert not firmware.any_attached()


def test_keepalive_resets_the_watchdog() -> None:
    clock = Clock()
    firmware = bench(clock)
    firmware.handle_line("E 100")
    for _ in range(5):
        clock.advance(1.0)
        assert firmware.handle_line("K") == ["OK"]
        firmware.tick()
    assert firmware.any_attached()
    assert not firmware.holding


def test_jaw_needs_a_jaw_channel() -> None:
    assert bench().handle_line("J 50") == ["ERR no jaw"]
    firmware = FakeFirmware(channels=BUST_CHANNELS)
    firmware.handle_line("E all")
    assert firmware.handle_line("J 100") == ["OK"]
    assert firmware.position(10) == 10  # interpolation just started
    firmware.handle_line("?")


def test_face_accepts_the_contract_emotions() -> None:
    firmware = bench()
    assert firmware.handle_line("F curious") == ["OK"]
    assert firmware.emotion == "curious"
    assert firmware.handle_line("F annoyed") == ["OK"]
    assert firmware.handle_line("F smug") == ["ERR unknown emotion"]


def test_errors_are_explicit() -> None:
    firmware = bench()
    assert firmware.handle_line("Z") == ["ERR unknown command"]
    assert firmware.handle_line("S 42 10") == ["ERR unknown channel"]
    assert firmware.handle_line("S 100") == ["ERR bad args"]
    assert firmware.handle_line("M 100:50") == ["ERR bad args"]
    assert firmware.handle_line("M 100:50 T99999") == ["ERR bad args"]


def test_duration_marker_is_case_insensitive() -> None:
    firmware = bench()
    firmware.handle_line("E 100")
    assert firmware.handle_line("M 100:50 t100") == ["OK"]
    assert firmware.handle_line("M 100:50 T100") == ["OK"]


def test_trailing_junk_is_refused() -> None:
    firmware = bench()
    assert firmware.handle_line("K junk") == ["ERR bad args"]
    assert firmware.handle_line("V junk") == ["ERR bad args"]
    assert firmware.handle_line("? junk") == ["ERR bad args"]
    assert firmware.handle_line("! junk") == ["ERR bad args"]
    assert firmware.handle_line("E 100 junk") == ["ERR bad args"]
    assert firmware.handle_line("M 100:50 T100 junk") == ["ERR bad args"]
    assert not firmware.estopped


def test_an_overlong_line_is_refused_once() -> None:
    firmware = bench()
    assert firmware.handle_line("F " + "x" * LINE_MAX) == ["ERR line too long"]
    # The tail of a rejected line is never interpreted as a command.
    assert firmware.commands == []


async def test_watchdog_notices_reach_the_tcp_client() -> None:
    clock = Clock()
    firmware = bench(clock)
    server = await serve_tcp(firmware)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(b"E 100\n")
        await writer.drain()
        while (await asyncio.wait_for(reader.readline(), 2.0)).strip() != b"OK":
            pass
        clock.advance(HOLD_AFTER_S + 0.1)
        firmware.tick()
        line = await asyncio.wait_for(reader.readline(), 2.0)
        assert line.strip() == b"# watchdog hold"
    finally:
        writer.close()
        server.close()
        await server.wait_closed()
