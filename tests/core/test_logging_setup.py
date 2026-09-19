"""Logging: records reach the ring buffer through the queue, noisy libs quiet."""

from __future__ import annotations

import logging
import time

from asimoov.core.logging_setup import NOISY_LOGGERS, RING_BUFFER_LINES, setup_logging


def drain(setup, needle: str, timeout_s: float = 2.0) -> list[str]:
    """Wait for the listener thread to have formatted ``needle``."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and not any(needle in line for line in setup.tail()):
        time.sleep(0.01)
    return setup.tail()


def test_records_land_in_the_ring_buffer():
    setup = setup_logging(logging.INFO, console=False)
    try:
        logging.getLogger("asimoov.test").info("hello ring")
        assert any("hello ring" in line for line in drain(setup, "hello ring"))
    finally:
        setup.stop()


def test_the_ring_buffer_is_bounded():
    setup = setup_logging(logging.INFO, console=False)
    try:
        logger = logging.getLogger("asimoov.test")
        for index in range(RING_BUFFER_LINES + 50):
            logger.info("line %d", index)
        lines = drain(setup, "line 549")
        assert len(lines) == RING_BUFFER_LINES
        assert "line 549" in lines[-1]
        assert not any("line 0 " in line for line in lines)
    finally:
        setup.stop()


def test_noisy_libraries_are_pinned_to_warning():
    setup = setup_logging(logging.DEBUG, console=False)
    try:
        for name in NOISY_LOGGERS:
            assert logging.getLogger(name).level == logging.WARNING
    finally:
        setup.stop()


def test_stop_detaches_the_queue_handler():
    setup = setup_logging(logging.INFO, console=False)
    setup.stop()
    assert setup.handler not in logging.getLogger().handlers
