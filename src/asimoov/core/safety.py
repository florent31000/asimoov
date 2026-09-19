"""`SafetyGuard`: watchdog, motion limits and an e-stop that never asks an LLM.

Independent of the body (plan.md section 4.5): it polls `body.health()`,
publishes it on ``body.health``, and cuts motion when the link goes quiet,
when a body that declares ``stop_on_disconnect`` disconnects, or when an
emergency phrase appears in an ``utterance``. The phrase check is a plain
string match: no model, no keyword-filtered transcript like Neon's
(plan.md section 2, item 6).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable

from asimoov.contracts.body import Body
from asimoov.contracts.vocab import TOPICS
from asimoov.core.config import DEFAULT_STOP_PHRASES
from asimoov.core.timeouts import run_with_timeout

log = logging.getLogger(__name__)

WATCHDOG_PERIOD_S = 1.0
WATCHDOG_MISSES = 3
DEFAULT_WATCHDOG_MS = 500
HEALTH_TIMEOUT_S = 1.0
#: How long the guard waits for the running behaviors to unwind before it
#: stops the body anyway. Bounded on purpose: safety never waits forever.
CANCEL_TIMEOUT_S = 1.0

StopHook = Callable[[str], Awaitable[None] | None]


def normalize_utterance(text: str) -> str:
    """Lowercase, strip accents and punctuation: the e-stop match is text-only."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip()


class SafetyGuard:
    """Watchdog and emergency stop for one body."""

    def __init__(
        self,
        body: Body,
        *,
        stop_phrases: tuple[str, ...] = DEFAULT_STOP_PHRASES,
        bus=None,
        on_stop: StopHook | None = None,
        watchdog_period_s: float = WATCHDOG_PERIOD_S,
        health_timeout_s: float = HEALTH_TIMEOUT_S,
    ) -> None:
        self.body = body
        self.bus = bus
        self.on_stop = on_stop
        self.stop_phrases = tuple(normalize_utterance(phrase) for phrase in stop_phrases)
        self.watchdog_period_s = watchdog_period_s
        self.health_timeout_s = health_timeout_s
        safety = body.manifest.safety or {}
        self.watchdog_ms = float(safety.get("watchdog_ms", DEFAULT_WATCHDOG_MS))
        self.stop_on_disconnect = bool(safety.get("stop_on_disconnect", False))
        self.max_continuous_motion_s = body.manifest.limits.get("max_continuous_motion_s")
        self.stops: list[str] = []
        self._task: asyncio.Task | None = None
        self._last_healthy: float | None = None
        self._was_connected = False

    async def start(self, spawn=None) -> None:
        """Start the watchdog, optionally under the runtime's `Supervisor`."""
        if spawn is not None:
            spawn("safety.watchdog", self.watch_forever)
            return
        if self._task is None:
            self._task = asyncio.create_task(self.watch_forever(), name="safety.watchdog")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def watchdog_budget_s(self) -> float:
        return WATCHDOG_MISSES * self.watchdog_ms / 1000

    def is_stop_phrase(self, text: str) -> bool:
        """True if ``text`` contains an emergency phrase as standalone words."""
        normalized = normalize_utterance(text)
        if not normalized:
            return False
        padded = f" {normalized} "
        return any(f" {phrase} " in padded for phrase in self.stop_phrases if phrase)

    async def check_utterance(self, text: str) -> bool:
        """Stop everything if ``text`` is an emergency phrase. Returns True if it did."""
        if not self.is_stop_phrase(text):
            return False
        await self.stop_all("e_stop_phrase")
        return True

    async def stop_all(self, reason: str) -> None:
        """Cut all motion now: cancel the behaviors, then stop the body.

        `on_stop` (the `BehaviorExecutor`) is awaited *first*, within a
        bounded budget: a behavior cancelled after `Body.stop_all` gets to
        push one last command into a body that has just been stopped, which
        is how a stopped robot moves again (review, medium).
        """
        self.stops.append(reason)
        log.warning("safety stop: %s", reason)
        if self.on_stop is not None:
            outcome = self.on_stop(reason)
            if asyncio.iscoroutine(outcome):
                cancelled, _ = await run_with_timeout(outcome, CANCEL_TIMEOUT_S)
                if not cancelled:
                    log.error(
                        "behaviors did not cancel within %.1fs, stopping the body anyway",
                        CANCEL_TIMEOUT_S,
                    )
        completed, _ = await run_with_timeout(self.body.stop_all(reason), HEALTH_TIMEOUT_S)
        if not completed:
            log.error("body.stop_all(%s) did not return within %.1fs", reason, HEALTH_TIMEOUT_S)
        if self.bus is not None:
            await self.bus.publish(
                TOPICS.BODY_REPLY, {"stopped": True, "reason": reason}, kind="reply"
            )

    async def watch_forever(self) -> None:
        while True:
            await asyncio.sleep(self.watchdog_period_s)
            now = time.monotonic()
            try:
                answered, health = await run_with_timeout(
                    self.body.health(), self.health_timeout_s
                )
                if not answered:
                    log.warning(
                        "body.health() did not answer within %.2fs", self.health_timeout_s
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("body.health() raised")
                health = None

            if health is not None and health.connected:
                self._last_healthy = now
                self._was_connected = True
                if self.bus is not None:
                    await self.bus.publish(TOPICS.BODY_HEALTH, health.to_dict(), kind="state")
                continue

            if health is not None and self.bus is not None:
                await self.bus.publish(TOPICS.BODY_HEALTH, health.to_dict(), kind="state")

            if not self._was_connected:
                # Never connected yet: `Body.start` returns before the link is
                # up, so this is startup, not a fault.
                continue
            if health is not None and not health.connected and self.stop_on_disconnect:
                self._was_connected = False
                await self.stop_all("disconnected")
                continue
            if self._last_healthy is not None and now - self._last_healthy > self.watchdog_budget_s:
                self._last_healthy = None
                await self.stop_all("watchdog")
