"""Logging: a QueueHandler feeding a 500-line ring buffer, noisy libs at WARNING.

Neon flooded its root logger at INFO and re-rendered a Kivy widget per line
(plan.md section 2, item 3). Here every record goes through a
`QueueHandler` (never blocking the event loop) into a bounded ring buffer
UIs poll at their own rate.
"""

from __future__ import annotations

import logging
import logging.handlers
import queue
from collections import deque
from collections.abc import Iterator

RING_BUFFER_LINES = 500
NOISY_LOGGERS: tuple[str, ...] = (
    "websockets",
    "websockets.client",
    "websockets.server",
    "aiortc",
    "aioice",
    "asyncio",
    "urllib3",
)
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class RingBufferHandler(logging.Handler):
    """Keeps the last `RING_BUFFER_LINES` formatted records in memory."""

    def __init__(self, capacity: int = RING_BUFFER_LINES) -> None:
        super().__init__()
        self.lines: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))

    def tail(self, count: int = RING_BUFFER_LINES) -> list[str]:
        return list(self.lines)[-count:]

    def __iter__(self) -> Iterator[str]:
        return iter(list(self.lines))


class LoggingSetup:
    """Handle returned by `setup_logging`, used to read the buffer and tear down."""

    def __init__(
        self,
        listener: logging.handlers.QueueListener,
        ring: RingBufferHandler,
        handler: logging.handlers.QueueHandler,
    ) -> None:
        self.listener = listener
        self.ring = ring
        self.handler = handler

    def tail(self, count: int = RING_BUFFER_LINES) -> list[str]:
        """Return the last ``count`` formatted log lines."""
        return self.ring.tail(count)

    def stop(self) -> None:
        """Stop the listener thread and detach the queue handler. Idempotent."""
        self.listener.stop()
        logging.getLogger().removeHandler(self.handler)


def setup_logging(level: int | str = logging.INFO, *, console: bool = True) -> LoggingSetup:
    """Route the root logger through a queue into a ring buffer (and stderr).

    Existing handlers on the root logger are replaced: a process that calls
    this owns its logging. ``NOISY_LOGGERS`` are pinned to WARNING.
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)

    formatter = logging.Formatter(LOG_FORMAT)
    ring = RingBufferHandler()
    ring.setFormatter(formatter)

    targets: list[logging.Handler] = [ring]
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        targets.append(stream)

    log_queue: queue.Queue = queue.Queue(-1)
    handler = logging.handlers.QueueHandler(log_queue)
    listener = logging.handlers.QueueListener(log_queue, *targets, respect_handler_level=True)
    listener.start()

    root.addHandler(handler)
    root.setLevel(level)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    return LoggingSetup(listener, ring, handler)
