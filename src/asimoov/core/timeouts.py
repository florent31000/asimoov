"""Bounded waits that never mistake a cancellation for a timeout.

`asyncio.wait_for` can surface an outer cancellation as `TimeoutError`
(CPython 3.11 rewrote it on top of `asyncio.timeout`, which calls
`Task.uncancel`). A supervised loop that catches `TimeoutError` then
swallows its own shutdown and keeps running. Everything in the core that
needs a deadline uses `run_with_timeout` instead, which reports the
timeout as a value and lets `CancelledError` through untouched.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, TypeVar

T = TypeVar("T")


async def run_with_timeout(awaitable: Awaitable[T], timeout_s: float) -> tuple[bool, Any]:
    """Await ``awaitable`` for at most ``timeout_s``.

    Returns ``(True, result)`` on success, ``(False, None)`` on timeout (the
    inner task is cancelled), and re-raises the awaitable's own exception.
    A cancellation of the caller propagates as `asyncio.CancelledError`.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_s)
    except asyncio.CancelledError:
        task.cancel()
        raise
    if not done:
        task.cancel()
        return False, None
    return True, task.result()
