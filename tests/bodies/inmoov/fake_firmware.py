"""Python twin of ``firmware/inmoov-uno-r4``: same commands, same replies.

`FakeFirmware` is the protocol itself (clamps, E-STOP latch, watchdog,
ease-in-out interpolation on a injectable clock). `FakeSerialPort` exposes it
as a pyserial-compatible object and `serve_tcp` as a TCP server, so the same
simulator backs both link flavours -- and WS1 can drive an InMoov body in
integration tests without any hardware.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

HOLD_AFTER_S = 2.0
DETACH_AFTER_S = 15.0
ESTOP_DETACH_S = 0.3
JAW_MS = 40
MAX_MOVE_MS = 20000
LINE_MAX = 160
NOTICE_POLL_S = 0.01

EMOTIONS = (
    "neutral",
    "happy",
    "excited",
    "curious",
    "annoyed",
    "sad",
    "angry",
    "love",
    "sleeping",
)


@dataclass(frozen=True)
class FakeChannel:
    """One entry of the firmware's ``config.h`` table."""

    id: int
    name: str
    min_deg: int
    max_deg: int
    rest_deg: int


BENCH_CHANNELS: tuple[FakeChannel, ...] = (FakeChannel(100, "finger_demo", 2, 108, 2),)

BUST_CHANNELS: tuple[FakeChannel, ...] = (
    FakeChannel(0, "fingers_r", 0, 120, 10),
    FakeChannel(1, "wrist_r", 0, 180, 90),
    FakeChannel(2, "elbow_r", 0, 150, 90),
    FakeChannel(3, "shoulder_r", 0, 150, 20),
    FakeChannel(8, "neck_yaw", 30, 150, 90),
    FakeChannel(9, "neck_pitch", 60, 120, 90),
    FakeChannel(10, "jaw", 10, 60, 10),
    FakeChannel(11, "eyelids", 20, 90, 20),
)


@dataclass
class _State:
    current: float
    start: float
    target: float
    t0: float = 0.0
    duration_ms: int = 0
    soft_min: int = 0
    soft_max: int = 180
    attached: bool = False


@dataclass
class FakeFirmware:
    """The line protocol, in Python."""

    channels: tuple[FakeChannel, ...] = BENCH_CHANNELS
    clock: Callable[[], float] = time.monotonic
    commands: list[str] = field(default_factory=list)
    emotion: str | None = None
    estopped: bool = False
    holding: bool = False
    notices: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._state = {
            channel.id: _State(
                current=float(channel.rest_deg),
                start=float(channel.rest_deg),
                target=float(channel.rest_deg),
                soft_min=channel.min_deg,
                soft_max=channel.max_deg,
            )
            for channel in self.channels
        }
        self._by_id = {channel.id: channel for channel in self.channels}
        self._estop_at = 0.0
        self.last_command_at = self.clock()

    # -- inspection ---------------------------------------------------------

    def position(self, channel_id: int) -> float:
        state = self._state[channel_id]
        if state.duration_ms:
            elapsed_ms = (self.clock() - state.t0) * 1000.0
            if elapsed_ms >= state.duration_ms:
                state.current = state.target
                state.duration_ms = 0
            else:
                u = elapsed_ms / state.duration_ms
                state.current = state.start + (state.target - state.start) * u * u * (3 - 2 * u)
        return state.current

    def attached(self, channel_id: int) -> bool:
        return self._state[channel_id].attached

    def any_attached(self) -> bool:
        return any(state.attached for state in self._state.values())

    def tick(self) -> None:
        """Run the watchdog, as ``loop()`` does at 50 Hz."""
        now = self.clock()
        if self.estopped and self.any_attached() and now - self._estop_at >= ESTOP_DETACH_S:
            self._detach_all()
            self.notices.append("# estop detached")
            return
        if not self.any_attached():
            return
        silence = now - self.last_command_at
        if silence >= DETACH_AFTER_S:
            self._detach_all()
            self.notices.append("# watchdog detach")
        elif silence >= HOLD_AFTER_S and not self.holding:
            self._freeze_all()
            self.holding = True
            self.notices.append("# watchdog hold")

    # -- protocol -----------------------------------------------------------

    def handle_line(self, line: str) -> list[str]:
        if len(line.rstrip("\r\n")) > LINE_MAX:
            # The board drops the whole overlong line, tail included, and
            # answers once: its tail is never taken for a command.
            return ["ERR line too long"]
        line = line.strip()
        if not line or line.startswith("#"):
            return []
        self.commands.append(line)
        self.last_command_at = self.clock()
        self.holding = False

        command = line[0].upper()
        argument = line[1:].strip()
        handler = {
            "?": self._cmd_state,
            "V": self._cmd_version,
            "E": lambda arg: self._cmd_attach(arg, True),
            "D": lambda arg: self._cmd_attach(arg, False),
            "P": self._cmd_declare,
            "S": self._cmd_set,
            "M": self._cmd_move,
            "L": self._cmd_limits,
            "J": self._cmd_jaw,
            "F": self._cmd_face,
            "K": self._cmd_keepalive,
            "!": self._cmd_estop,
        }.get(command)
        if handler is None:
            return ["ERR unknown command"]
        return handler(argument)  # type: ignore[operator]

    def _cmd_state(self, argument: str) -> list[str]:
        if argument:
            return ["ERR bad args"]
        lines = []
        for channel in self.channels:
            state = self._state[channel.id]
            position = round(self.position(channel.id))
            lines.append(
                f"ST {channel.id} {channel.name} {position} {state.soft_min} "
                f"{state.soft_max} {int(state.attached)} {int(bool(state.duration_ms))}"
            )
        return [*lines, "END"]

    def _cmd_version(self, argument: str) -> list[str]:
        if argument:
            return ["ERR bad args"]
        lines = [f"V asimoov-inmoov 1.0.0 1 uno_r4_wifi {len(self.channels)}"]
        for channel in self.channels:
            state = self._state[channel.id]
            lines.append(
                f"CH {channel.id} {channel.name} {state.soft_min} "
                f"{state.soft_max} {channel.rest_deg}"
            )
        return [*lines, "END"]

    def _cmd_attach(self, argument: str, enable: bool) -> list[str]:
        if argument.lower() == "all":
            targets = list(self._state)
        else:
            channel_id = _int(argument)
            if channel_id is None:
                return ["ERR bad args"]
            if channel_id not in self._state:
                return ["ERR unknown channel"]
            targets = [channel_id]
        for channel_id in targets:
            self._state[channel_id].attached = enable
        if enable:
            self.estopped = False
        return ["OK"]

    def _cmd_declare(self, argument: str) -> list[str]:
        parts = argument.split()
        if len(parts) != 2:
            return ["ERR bad args"]
        channel_id, degrees = _int(parts[0]), _int(parts[1])
        if channel_id is None or degrees is None:
            return ["ERR bad args"]
        if channel_id not in self._state:
            return ["ERR unknown channel"]
        state = self._state[channel_id]
        value = float(min(max(degrees, state.soft_min), state.soft_max))
        state.current = state.start = state.target = value
        state.duration_ms = 0
        return ["OK"]

    def _cmd_set(self, argument: str) -> list[str]:
        if self.estopped:
            return ["ERR estop"]
        parts = argument.split()
        if len(parts) != 2:
            return ["ERR bad args"]
        channel_id, degrees = _int(parts[0]), _int(parts[1])
        if channel_id is None or degrees is None:
            return ["ERR bad args"]
        if channel_id not in self._state:
            return ["ERR unknown channel"]
        self._start_move(channel_id, degrees, 0)
        return ["OK"]

    def _cmd_move(self, argument: str) -> list[str]:
        if self.estopped:
            return ["ERR estop"]
        # The `T` marker is case-insensitive, like the command letter.
        marker = argument.upper().find("T")
        if marker < 0:
            return ["ERR bad args"]
        body, duration_text = argument[:marker], argument[marker + 1 :]
        duration = _int(duration_text)
        if duration is None or duration < 0 or duration > MAX_MOVE_MS:
            return ["ERR bad args"]
        pairs = []
        for item in body.replace(" ", "").split(","):
            if not item:
                continue
            channel_text, colon, degrees_text = item.partition(":")
            channel_id, degrees = _int(channel_text), _int(degrees_text)
            if not colon or channel_id is None or degrees is None:
                return ["ERR bad args"]
            if channel_id not in self._state:
                return ["ERR unknown channel"]
            pairs.append((channel_id, degrees))
        if not pairs:
            return ["ERR bad args"]
        for channel_id, degrees in pairs:
            self._start_move(channel_id, degrees, duration)
        return ["OK"]

    def _cmd_limits(self, argument: str) -> list[str]:
        parts = argument.split()
        if len(parts) != 3:
            return ["ERR bad args"]
        channel_id, low, high = (_int(part) for part in parts)
        if channel_id is None or low is None or high is None or low > high:
            return ["ERR bad args"]
        if channel_id not in self._state:
            return ["ERR unknown channel"]
        channel = self._by_id[channel_id]
        state = self._state[channel_id]
        state.soft_min = max(low, channel.min_deg)
        state.soft_max = min(high, channel.max_deg)
        state.current = min(max(state.current, state.soft_min), state.soft_max)
        state.target = min(max(state.target, state.soft_min), state.soft_max)
        return ["OK"]

    def _cmd_jaw(self, argument: str) -> list[str]:
        if self.estopped:
            return ["ERR estop"]
        jaw = next((channel for channel in self.channels if channel.name == "jaw"), None)
        if jaw is None:
            return ["ERR no jaw"]
        amount = _int(argument)
        if amount is None:
            return ["ERR bad args"]
        amount = min(max(amount, 0), 100)
        state = self._state[jaw.id]
        degrees = state.soft_min + (state.soft_max - state.soft_min) * amount / 100.0
        self._start_move(jaw.id, degrees, JAW_MS)
        return ["OK"]

    def _cmd_face(self, argument: str) -> list[str]:
        if argument.lower() not in EMOTIONS:
            return ["ERR unknown emotion"]
        self.emotion = argument.lower()
        return ["OK"]

    def _cmd_keepalive(self, argument: str) -> list[str]:
        return ["ERR bad args"] if argument else ["OK"]

    def _cmd_estop(self, argument: str) -> list[str]:
        if argument:
            return ["ERR bad args"]
        self._freeze_all()
        self.estopped = True
        self._estop_at = self.clock()
        return ["OK"]

    # -- internals ----------------------------------------------------------

    def _start_move(self, channel_id: int, degrees: float, duration_ms: int) -> None:
        state = self._state[channel_id]
        state.start = self.position(channel_id)
        state.target = float(min(max(degrees, state.soft_min), state.soft_max))
        state.t0 = self.clock()
        state.duration_ms = duration_ms
        if not duration_ms:
            state.current = state.target

    def _freeze_all(self) -> None:
        for channel_id, state in self._state.items():
            state.current = self.position(channel_id)
            state.target = state.current
            state.duration_ms = 0

    def _detach_all(self) -> None:
        for state in self._state.values():
            state.attached = False


def _int(text: str) -> int | None:
    try:
        return int(text.strip())
    except (TypeError, ValueError):
        return None


class FakeSerialPort:
    """Minimal pyserial-compatible port backed by a `FakeFirmware`."""

    def __init__(self, firmware: FakeFirmware, *, timeout: float = 0.1) -> None:
        self.firmware = firmware
        self.timeout = timeout
        self._out = bytearray(b"# asimoov-inmoov 1.0.0 ready, all servos detached\n")
        self._in = bytearray()
        self._condition = threading.Condition()
        self._closed = False
        self._seen_notices = 0

    def _pump_notices(self) -> None:
        pending = self.firmware.notices[self._seen_notices :]
        self._seen_notices += len(pending)
        for notice in pending:
            self._out.extend(notice.encode("ascii") + b"\n")

    def read(self, size: int = 1) -> bytes:
        with self._condition:
            self._pump_notices()
            if not self._out and not self._closed:
                self._condition.wait(self.timeout)
                self._pump_notices()
            if self._closed:
                raise OSError("port closed")
            chunk = bytes(self._out[:size])
            del self._out[: len(chunk)]
            return chunk

    def write(self, payload: bytes) -> int:
        with self._condition:
            if self._closed:
                raise OSError("port closed")
            self._in.extend(payload)
            while b"\n" in self._in:
                raw, _, rest = bytes(self._in).partition(b"\n")
                self._in = bytearray(rest)
                for reply in self.firmware.handle_line(raw.decode("ascii", "replace")):
                    self._out.extend(reply.encode("ascii") + b"\n")
            self._condition.notify_all()
            return len(payload)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()


async def make_tcp_body(firmware: FakeFirmware, **config: object):
    """Build an `InMoovBody` talking to ``firmware`` over a real local socket."""
    from asimoov.bodies.inmoov.adapter import InMoovBody

    server = await serve_tcp(firmware)
    port = server.sockets[0].getsockname()[1]
    body = InMoovBody({"link": "tcp", "host": "127.0.0.1", "port": port, **config})
    return body, server


async def serve_tcp(
    firmware: FakeFirmware, host: str = "127.0.0.1", port: int = 0
) -> asyncio.Server:
    """Serve ``firmware`` on a TCP port, like ``WiFiServer(5005)`` does.

    Unsolicited notices (watchdog, e-stop) are pushed to the connected client
    too, as the firmware's ``notice()`` does.
    """

    async def push_notices(writer: asyncio.StreamWriter) -> None:
        seen = len(firmware.notices)
        while True:
            await asyncio.sleep(NOTICE_POLL_S)
            pending = firmware.notices[seen:]
            if not pending:
                continue
            seen += len(pending)
            for notice in pending:
                writer.write(notice.encode("ascii") + b"\n")
            await writer.drain()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"# asimoov-inmoov connected\n")
        await writer.drain()
        pump = asyncio.create_task(push_notices(writer))
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                for reply in firmware.handle_line(raw.decode("ascii", "replace")):
                    writer.write(reply.encode("ascii") + b"\n")
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            writer.close()

    return await asyncio.start_server(handle, host, port)
