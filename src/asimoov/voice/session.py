"""Realtime session lifecycle: truncation, hot renewal, idle disconnection.

Neon never truncated nor renewed its Realtime session, so latency grew with
every minute of conversation (plan.md section 2, item 2). `SessionManager`
watches tokens, age and inactivity, deletes conversation items beyond a cap,
and renews the session in a double buffer: an out-of-band summary
(``response.create`` with ``conversation: none``, text output) is carried into
a second socket that only becomes active once it is connected, at the next
silence, before the old one is closed -- no audio gap.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from asimoov.contracts.voice import VoiceEvents
from asimoov.core.session import (
    IDLE_DISCONNECT_S,
    MAX_ITEMS,
    RENEW_AGE_S,
    RENEW_TOKENS,
    SessionState,
    SessionThresholds,
)

log = logging.getLogger(__name__)

DEFAULT_SUMMARY_INSTRUCTIONS = (
    "Summarize the conversation so far in at most 120 words, in the language being "
    "spoken: who is present, what they asked, what you did or promised, and anything "
    "you must remember. No preamble."
)
SUMMARY_HEADER = "[Previous conversation summary]"
SCENE_HEADER = "[Scene]"


@dataclass(frozen=True)
class SessionLimits:
    """`core.session.SessionThresholds` plus the budgets a hot renewal needs.

    The four thresholds are not re-decided here: they come from
    `core/session.py`, which owns the policy and is where they are tested.
    """

    max_total_tokens: int = RENEW_TOKENS
    max_age_s: float = RENEW_AGE_S
    idle_timeout_s: float = IDLE_DISCONNECT_S
    max_items: int = MAX_ITEMS
    summary_timeout_s: float = 15.0
    switch_timeout_s: float = 10.0
    retry_after_s: float = 60.0

    def thresholds(self) -> SessionThresholds:
        return SessionThresholds(
            max_total_tokens=self.max_total_tokens,
            max_age_s=self.max_age_s,
            idle_disconnect_s=self.idle_timeout_s,
            max_items=self.max_items,
        )


class RenewableProvider(Protocol):
    """The slice of `openai_realtime.OpenAIRealtimeProvider` renewal needs."""

    async def start(self, events: VoiceEvents, config: dict[str, Any]) -> None: ...

    async def stop(self) -> None: ...

    async def wait_ready(self, timeout_s: float = ...) -> None: ...

    async def request_summary(self, instructions: str, *, timeout_s: float = ...) -> str: ...

    async def prune_items(self, max_items: int = ...) -> list[str]: ...


class SessionManager:
    """Owns the live `VoiceProvider` and replaces it before it degrades.

    Args:
        factory: Builds a fresh, unstarted provider (same `VoiceEvents` are
            reused; the core does not have to rewire anything).
        config: The ``start`` config; ``instructions`` is extended with the
            summary on renewal.
        events: Passed through to every provider this manager starts.
        is_silent: Returns True when nothing is playing and no response is
            active. The switch waits for it.
        context_provider: Optional current-scene text appended to the renewed
            instructions.
        on_summary: Called with the summary text (the core writes an episode).
        on_renewal: Called with the reason once a renewal completed
            (``session_renewals`` telemetry).
    """

    def __init__(
        self,
        factory: Callable[[], RenewableProvider],
        config: dict[str, Any],
        events: VoiceEvents,
        *,
        limits: SessionLimits | None = None,
        is_silent: Callable[[], bool] | None = None,
        summary_instructions: str = DEFAULT_SUMMARY_INSTRUCTIONS,
        context_provider: Callable[[], str] | None = None,
        on_summary: Callable[[str], None] | None = None,
        on_renewal: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = factory
        self._config = dict(config)
        self._events = events
        self._limits = limits or SessionLimits()
        self._is_silent = is_silent or (lambda: True)
        self._summary_instructions = summary_instructions
        self._context_provider = context_provider
        self._on_summary = on_summary
        self._on_renewal = on_renewal
        self._clock = clock

        self._active: RenewableProvider | None = None
        self._state: SessionState | None = None
        self._renewals = 0
        self._retry_at = 0.0

    # ------------------------------------------------------------- state

    @property
    def active(self) -> RenewableProvider | None:
        return self._active

    @property
    def renewals(self) -> int:
        return self._renewals

    @property
    def total_tokens(self) -> int:
        return self._state.total_tokens if self._state is not None else 0

    def age_s(self) -> float:
        return self._clock() - self._state.started_at if self._state is not None else 0.0

    def idle_s(self) -> float:
        return self._clock() - self._state.last_activity_at if self._state is not None else 0.0

    def note_activity(self) -> None:
        """Called by the core on any speech, response or tool call."""
        if self._state is not None:
            self._state.touch(self._clock())

    def note_usage(self, usage: dict[str, Any] | int) -> None:
        """Record ``usage.total_tokens`` from ``response.done`` (a high-water mark)."""
        if self._state is None:
            return
        total = usage if isinstance(usage, int) else usage.get("total_tokens", 0)
        self._state.total_tokens = max(self._state.total_tokens, int(total))

    def renewal_reason(self) -> str | None:
        """Why the session must be renewed now, per `core.session`."""
        if self._state is None:
            return None
        return self._state.renew_reason(self._clock())

    def is_idle(self) -> bool:
        return self._state is not None and self._state.should_disconnect(self._clock())

    # ----------------------------------------------------------- lifecycle

    async def start(self) -> RenewableProvider:
        """Start the first session. Idempotent while one is active."""
        if self._active is not None:
            return self._active
        provider = self._factory()
        await provider.start(self._events, dict(self._config))
        self._active = provider
        self._state = self._fresh_state()
        return provider

    async def stop(self) -> None:
        provider, self._active = self._active, None
        self._state = None
        if provider is not None:
            await provider.stop()

    def update_config(self, config: dict[str, Any]) -> None:
        """Replace the config the next session starts with (rebound tools)."""
        self._config = dict(config)

    async def tick(self) -> None:
        """Called by the core roughly once a second."""
        provider = self._active
        if provider is None:
            return
        await provider.prune_items(self._limits.max_items)
        if self.is_idle():
            log.info("voice session idle for %.0f s, disconnecting", self.idle_s())
            await self.stop()
            return
        reason = self.renewal_reason()
        if reason is None or self._clock() < self._retry_at:
            return
        try:
            await self.renew(reason)
        except Exception:
            self._retry_at = self._clock() + self._limits.retry_after_s
            log.exception("session renewal failed, retrying in %.0f s", self._limits.retry_after_s)

    async def renew(self, reason: str = "manual") -> RenewableProvider:
        """Renew the session in a double buffer, without an audio gap.

        Raises:
            RuntimeError: if no session is active.
            TimeoutError: if the summary or the new session times out; the old
                session keeps running and `tick` retries later.
        """
        old = self._active
        if old is None:
            raise RuntimeError("no active session to renew")
        # The summary is an out-of-band `response.create`; asking for it in
        # the middle of a spoken answer races the response being streamed.
        await self._wait_for_silence()
        summary = await old.request_summary(
            self._summary_instructions, timeout_s=self._limits.summary_timeout_s
        )
        if self._on_summary is not None:
            self._on_summary(summary)

        new = self._factory()
        await new.start(self._events, self._renewed_config(summary))
        try:
            await new.wait_ready(self._limits.switch_timeout_s)
        except TimeoutError:
            await new.stop()
            raise

        await self._wait_for_silence()
        self._active = new
        self._state = self._fresh_state()
        self._renewals += 1
        await old.stop()
        log.info("voice session renewed (%s), renewal #%d", reason, self._renewals)
        if self._on_renewal is not None:
            self._on_renewal(reason)
        return new

    # ----------------------------------------------------------- internals

    def _fresh_state(self) -> SessionState:
        now = self._clock()
        return SessionState(
            started_at=now, last_activity_at=now, thresholds=self._limits.thresholds()
        )

    def _renewed_config(self, summary: str) -> dict[str, Any]:
        config = dict(self._config)
        parts = [config.get("instructions", "")]
        if summary:
            parts.append(f"{SUMMARY_HEADER}\n{summary}")
        if self._context_provider is not None:
            context = self._context_provider()
            if context:
                parts.append(f"{SCENE_HEADER}\n{context}")
        config["instructions"] = "\n\n".join(part for part in parts if part)
        return config

    async def _wait_for_silence(self) -> None:
        deadline = self._clock() + self._limits.switch_timeout_s
        while not self._is_silent():
            if self._clock() >= deadline:
                log.warning("switching sessions without waiting for silence")
                return
            await asyncio.sleep(0.05)
