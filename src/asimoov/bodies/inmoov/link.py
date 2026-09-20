"""Serial and TCP links to the InMoov firmware.

Both speak the same line protocol (``firmware/inmoov-uno-r4/protocol.md``):
one command per line, one reply per command, terminated by ``OK``,
``ERR <msg>`` or ``END``. `Link` owns the framing, the single in-flight
command, the 1 Hz keepalive, the reconnect loop and the E-STOP latch;
`SerialLink` and `TcpLink` only move bytes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from asimoov.core.timeouts import run_with_timeout

LOG = logging.getLogger(__name__)

REPLY_TIMEOUT_S = 0.5
KEEPALIVE_INTERVAL_S = 1.0
RECONNECT_DELAY_S = 1.0
CONNECT_TIMEOUT_S = 2.0
TCP_PORT = 5005
BAUDRATE = 115200
REARM_COMMAND = "E all"
ESTOP_LINE = "!"
# The only commands the firmware answers `ERR estop` to, hence the only ones
# worth re-arming for: a `?` or a `V` works fine while the latch is set.
ESTOP_REFUSED = frozenset("SMJ")


@dataclass(frozen=True)
class Reply:
    """Answer to one command.

    ``status`` is ``OK``, ``ERR``, ``END``, or the empty string when the
    firmware did not answer in time (or the link was down); ``lines`` holds
    the payload lines of a multi-line answer (``ST ...``, ``CH ...``).
    """

    status: str
    lines: tuple[str, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("OK", "END")


@dataclass(frozen=True)
class ChannelSpec:
    """One servo channel, as declared by the firmware's ``V`` answer."""

    id: int
    name: str
    min_deg: int
    max_deg: int
    rest_deg: int

    def clamp(self, deg: float) -> int:
        return int(round(min(max(deg, self.min_deg), self.max_deg)))


def parse_channels(lines: tuple[str, ...]) -> dict[str, ChannelSpec]:
    """Parse the ``CH <id> <name> <min> <max> <rest>`` lines of a ``V`` answer."""
    channels: dict[str, ChannelSpec] = {}
    for line in lines:
        parts = line.split()
        if len(parts) != 6 or parts[0] != "CH":
            continue
        channels[parts[2]] = ChannelSpec(
            id=int(parts[1]),
            name=parts[2],
            min_deg=int(parts[3]),
            max_deg=int(parts[4]),
            rest_deg=int(parts[5]),
        )
    return channels


class Link(ABC):
    """Shared framing, keepalive and reconnect logic for both transports.

    ``on_connect`` is awaited every time the link comes up (the adapter uses
    it to re-read ``V`` and re-apply its ``L`` limits, since a reconnect
    usually means the board rebooted with its `config.h` defaults);
    ``on_disconnect`` is awaited every time it goes down.

    The E-STOP latch is tracked here because every caller -- gestures, gaze,
    jaw, face -- goes through `send`: a ``!`` or an ``ERR estop`` sets it,
    and the next command the firmware would refuse re-arms with ``E all``
    first. Without that, only a gesture ever cleared the latch and the
    5 Hz loops stayed refused forever.
    """

    def __init__(
        self,
        *,
        on_connect: Callable[[], Awaitable[None]] | None = None,
        on_disconnect: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._lock = asyncio.Lock()
        # Held only while a line is written, so an E-STOP never waits for the
        # reply of the command in flight but still cannot overtake its write.
        self._write_lock = asyncio.Lock()
        # Replies come back in the order the commands were written, so one
        # FIFO of awaited replies is enough to pair them -- including the
        # `!` of an E-STOP, which jumps the queue and is pushed as None.
        self._queue: deque[asyncio.Future[Reply] | None] = deque()
        self._payload: list[str] = []
        self._connected = False
        self._closing = True
        self._estopped = False
        self._down = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._maintain_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self.last_rtt_ms: float | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def estopped(self) -> bool:
        """True while the firmware's E-STOP latch is known to be set."""
        return self._estopped

    def set_callbacks(
        self,
        *,
        on_connect: Callable[[], Awaitable[None]] | None = None,
        on_disconnect: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Attach the connect/disconnect callbacks to an injected link."""
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect

    async def start(self) -> None:
        """Start connecting. Returns immediately; the link comes up later."""
        if self._maintain_task is not None and not self._maintain_task.done():
            return
        self._loop = asyncio.get_running_loop()
        self._closing = False
        self._down.clear()
        self._maintain_task = asyncio.create_task(self._maintain(), name="inmoov-link")

    async def close(self) -> None:
        """Close the link and stop reconnecting. Idempotent."""
        self._closing = True
        self._down.set()
        task = self._maintain_task
        self._maintain_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._teardown()

    async def send(self, line: str, *, timeout_s: float = REPLY_TIMEOUT_S) -> Reply:
        """Send one command and wait for its reply. One command in flight.

        A latched E-STOP is cleared first with ``E all`` when ``line`` is one
        the firmware would refuse, whoever the caller is.
        """
        async with self._lock:
            if not self._connected or self._loop is None:
                return Reply("", error="link down")
            if self._estopped and line[:1].upper() in ESTOP_REFUSED:
                rearm = await self._exchange(REARM_COMMAND, timeout_s)
                if not rearm.ok:
                    LOG.warning("inmoov link: re-arm refused (%s)", rearm.error)
                    return rearm
                self._estopped = False
            reply = await self._exchange(line, timeout_s)
            if reply.status == "ERR" and reply.error == "estop":
                self._estopped = True
            elif reply.ok and line[:1].upper() == "E":
                self._estopped = False
            return reply

    async def _exchange(self, line: str, timeout_s: float) -> Reply:
        assert self._loop is not None
        future: asyncio.Future[Reply] = self._loop.create_future()
        started = time.monotonic()
        async with self._write_lock:
            self._queue.append(future)
            try:
                await self._write((line + "\n").encode("ascii"))
            except (OSError, RuntimeError) as exc:
                self._drop(future)
                self._mark_down()
                return Reply("", error=f"write failed: {exc}")
        answered, reply = await run_with_timeout(future, timeout_s)
        if not answered:
            # Leave the future in the queue: a late reply belongs to this
            # command and must not be paired with the next one.
            return Reply("", error=f"no reply to {line!r} within {timeout_s}s")
        self.last_rtt_ms = (time.monotonic() - started) * 1000.0
        return reply

    async def send_priority(self, line: str) -> None:
        """Write a line without waiting for its reply nor for the in-flight
        command: the E-STOP must never queue behind a running move.
        """
        if not self._connected:
            return
        if line == ESTOP_LINE:
            self._estopped = True
        async with self._write_lock:
            self._queue.append(None)
            try:
                await self._write((line + "\n").encode("ascii"))
            except (OSError, RuntimeError) as exc:
                self._drop(None)
                LOG.warning("inmoov link: priority write failed: %s", exc)
                self._mark_down()

    def simulate_drop(self) -> None:
        """Drop the link as a cable unplug would, for tests and drills."""
        self._mark_down()

    # -- framing ------------------------------------------------------------

    def _feed_line(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if text.startswith("#"):
            LOG.debug("inmoov firmware: %s", text)
            return
        status, error = self._terminator(text)
        if status is None:
            self._payload.append(text)
            return
        future = self._queue.popleft() if self._queue else None
        if future is not None and not future.done():
            future.set_result(Reply(status, tuple(self._payload), error))
        self._payload = []

    @staticmethod
    def _terminator(text: str) -> tuple[str | None, str | None]:
        if text in ("OK", "END"):
            return text, None
        if text == "ERR" or text.startswith("ERR "):
            return "ERR", text[4:].strip() or "error"
        return None, None

    def _drop(self, entry: asyncio.Future[Reply] | None) -> None:
        with contextlib.suppress(ValueError):
            self._queue.remove(entry)

    def _mark_down(self) -> None:
        self._down.set()

    def _call_soon(self, callback: Callable[..., None], *args: object) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(callback, *args)

    # -- lifecycle ----------------------------------------------------------

    async def _maintain(self) -> None:
        while not self._closing:
            try:
                await self._open()
            except (OSError, asyncio.TimeoutError, ImportError) as exc:
                LOG.warning("inmoov link: connect failed: %s", exc)
                await asyncio.sleep(RECONNECT_DELAY_S)
                continue

            self._down.clear()
            self._connected = True
            if self._on_connect is not None:
                await self._on_connect()
            self._keepalive_task = asyncio.create_task(
                self._keepalive(), name="inmoov-keepalive"
            )

            await self._down.wait()

            self._connected = False
            await self._teardown()
            if self._on_disconnect is not None:
                await self._on_disconnect()
            if self._closing:
                return
            await asyncio.sleep(RECONNECT_DELAY_S)

    async def _teardown(self) -> None:
        task = self._keepalive_task
        self._keepalive_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._connected = False
        self._estopped = False
        while self._queue:
            future = self._queue.popleft()
            if future is not None and not future.done():
                future.set_result(Reply("", error="link down"))
        self._payload = []
        with contextlib.suppress(OSError, RuntimeError):
            await self._close()

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_S)
            reply = await self.send("K")
            if not reply.ok:
                LOG.warning("inmoov link: keepalive failed (%s)", reply.error)
                self._mark_down()
                return

    # -- transport ----------------------------------------------------------

    @abstractmethod
    async def _open(self) -> None:
        """Open the transport. Raises `OSError` if it cannot connect."""

    @abstractmethod
    async def _write(self, payload: bytes) -> None:
        """Write raw bytes to the transport."""

    @abstractmethod
    async def _close(self) -> None:
        """Close the transport. Idempotent."""


class TcpLink(Link):
    """Link to ``WiFiServer(5005)`` on the board."""

    def __init__(self, host: str, port: int = TCP_PORT, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task[None] | None = None

    async def _open(self) -> None:
        # `run_with_timeout`, not `wait_for`: `_maintain` catches
        # `TimeoutError` to retry, and on CPython 3.11+ `wait_for` reports an
        # outer cancellation as one -- the reconnect loop would then swallow
        # its own shutdown and never stop.
        connected, streams = await run_with_timeout(
            asyncio.open_connection(self._host, self._port), CONNECT_TIMEOUT_S
        )
        if not connected:
            raise asyncio.TimeoutError(f"no answer from {self._host}:{self._port}")
        self._reader, self._writer = streams
        self._read_task = asyncio.create_task(self._read_loop(), name="inmoov-tcp-read")

    async def _read_loop(self) -> None:
        reader = self._reader
        assert reader is not None
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                self._feed_line(raw.decode("ascii", "replace"))
        except (OSError, asyncio.IncompleteReadError):
            LOG.exception("inmoov tcp: read loop failed")
        finally:
            self._mark_down()

    async def _write(self, payload: bytes) -> None:
        if self._writer is None:
            raise OSError("tcp link not open")
        self._writer.write(payload)
        await self._writer.drain()

    async def _close(self) -> None:
        task = self._read_task
        self._read_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError, asyncio.TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)


class SerialLink(Link):
    """Link to the USB serial port, with a reader thread.

    ``pyserial`` (the ``[inmoov]`` extra) is imported lazily, so importing
    this module on a machine without it -- or with a ``serial_factory``
    injected by a test -- costs nothing.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = BAUDRATE,
        *,
        serial_factory: Callable[[], object] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._port = port
        self._baudrate = baudrate
        self._serial_factory = serial_factory or self._open_pyserial
        self._serial: object | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _open_pyserial(self) -> object:
        import serial  # noqa: PLC0415 -- optional [inmoov] dependency

        return serial.Serial(self._port, self._baudrate, timeout=0.1)

    async def _open(self) -> None:
        self._serial = await asyncio.to_thread(self._serial_factory)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._read_loop, name="inmoov-serial-read", daemon=True
        )
        self._thread.start()

    def _read_loop(self) -> None:
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = self._serial.read(64)  # type: ignore[union-attr]
            except Exception as exc:  # pyserial raises SerialException on unplug
                LOG.warning("inmoov serial: read failed: %s", exc)
                self._call_soon(self._mark_down)
                return
            if not chunk:
                continue
            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                self._call_soon(self._feed_line, raw.decode("ascii", "replace"))

    async def _write(self, payload: bytes) -> None:
        # pyserial writes block on the OS buffer: off the loop, one at a time.
        # `Link._write_lock` is what keeps the write order the reply FIFO
        # relies on, not the fact of writing inline.
        serial_port = self._serial
        if serial_port is None:
            raise OSError("serial port not open")
        await asyncio.to_thread(serial_port.write, payload)  # type: ignore[union-attr]

    async def _close(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        serial_port = self._serial
        self._serial = None
        if serial_port is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(serial_port.close)  # type: ignore[union-attr]
        if thread is not None:
            await asyncio.to_thread(thread.join, 1.0)
