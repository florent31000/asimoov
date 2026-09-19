"""SafetyGuard: e-stop phrases, watchdog, disconnect, motion limit."""

from __future__ import annotations

import asyncio

from asimoov.contracts.body import BodyHealth, BodyManifest
from asimoov.contracts.fakes import FakeBody
from asimoov.core.bus.local import LocalBus
from asimoov.core.safety import SafetyGuard

WATCHDOG = BodyManifest(
    name="watched",
    kind_of_body="quadruped",
    capabilities=("locomotion.planar",),
    limits={"max_continuous_motion_s": 20},
    safety={"watchdog_ms": 50, "stop_on_disconnect": True},
)


class FlakyBody(FakeBody):
    """A body whose health can be made to hang or report a disconnect."""

    def __init__(self, manifest=WATCHDOG):
        super().__init__(manifest=manifest)
        self.hang = False
        self.disconnected = False

    async def health(self) -> BodyHealth:
        if self.hang:
            await asyncio.sleep(10)
        return BodyHealth(connected=not self.disconnected)


def test_stop_phrases_are_matched_as_whole_words():
    guard = SafetyGuard(FakeBody())
    assert guard.is_stop_phrase("Stop!") is True
    assert guard.is_stop_phrase("Arrête-toi tout de suite") is True
    assert guard.is_stop_phrase("arrete tout") is True
    assert guard.is_stop_phrase("I went to the bus stop yesterday") is True
    assert guard.is_stop_phrase("Do not stopper the flow") is False
    assert guard.is_stop_phrase("") is False


def test_custom_stop_phrases():
    guard = SafetyGuard(FakeBody(), stop_phrases=("freeze",))
    assert guard.is_stop_phrase("freeze") is True
    assert guard.is_stop_phrase("stop") is False


async def test_an_emergency_phrase_stops_the_body_without_an_llm():
    body = FakeBody()
    guard = SafetyGuard(body)
    assert await guard.check_utterance("Stop, stop!") is True
    assert body.last_stop_all_reason == "e_stop_phrase"
    assert await guard.check_utterance("how are you?") is False


async def test_stop_all_runs_the_hook_and_publishes():
    body = FakeBody()
    bus = LocalBus()
    published: list[dict] = []

    async def handler(envelope):
        published.append(envelope.data)

    bus.subscribe("body.reply", handler)
    reasons: list[str] = []

    async def on_stop(reason: str) -> None:
        reasons.append(reason)

    guard = SafetyGuard(body, bus=bus, on_stop=on_stop)
    await guard.stop_all("tool")
    assert reasons == ["tool"]
    assert published == [{"stopped": True, "reason": "tool"}]
    assert guard.stops == ["tool"]


async def test_the_watchdog_stops_a_silent_body():
    body = FlakyBody()
    guard = SafetyGuard(body, watchdog_period_s=0.02, health_timeout_s=0.05)
    await guard.start()
    try:
        await asyncio.sleep(0.08)  # long enough to be seen healthy once
        body.hang = True
        for _ in range(200):
            if body.last_stop_all_reason:
                break
            await asyncio.sleep(0.01)
    finally:
        await guard.stop()
    assert body.last_stop_all_reason == "watchdog"


async def test_a_disconnect_stops_a_body_that_declares_it():
    body = FlakyBody()
    guard = SafetyGuard(body, watchdog_period_s=0.02)
    await guard.start()
    try:
        await asyncio.sleep(0.1)
        body.disconnected = True
        for _ in range(100):
            if body.last_stop_all_reason:
                break
            await asyncio.sleep(0.01)
    finally:
        await guard.stop()
    assert body.last_stop_all_reason == "disconnected"


async def test_a_body_that_never_connected_is_not_a_fault():
    body = FlakyBody()
    body.disconnected = True
    guard = SafetyGuard(body, watchdog_period_s=0.02)
    await guard.start()
    try:
        await asyncio.sleep(0.15)
    finally:
        await guard.stop()
    assert body.last_stop_all_reason is None


async def test_health_is_published_on_the_bus():
    bus = LocalBus()
    guard = SafetyGuard(FlakyBody(), bus=bus, watchdog_period_s=0.02)
    await guard.start()
    try:
        for _ in range(100):
            if bus.latest("body.health") is not None:
                break
            await asyncio.sleep(0.01)
    finally:
        await guard.stop()
    assert bus.latest("body.health").data["connected"] is True


def test_the_motion_limit_comes_from_the_manifest():
    assert SafetyGuard(FlakyBody()).max_continuous_motion_s == 20
    assert SafetyGuard(FakeBody()).watchdog_budget_s == 1.5


async def test_behaviors_are_cancelled_before_the_body_is_stopped():
    """A behavior cancelled after `stop_all` pushes one last command."""
    order: list[str] = []

    class RecordingBody(FakeBody):
        async def stop_all(self, reason: str) -> None:
            order.append("body.stop_all")
            await super().stop_all(reason)

    async def cancel_behaviors(reason: str) -> None:
        await asyncio.sleep(0)
        order.append("behaviors.cancel")

    guard = SafetyGuard(RecordingBody(), on_stop=cancel_behaviors)
    await guard.stop_all("tool")
    assert order == ["behaviors.cancel", "body.stop_all"]


async def test_a_behavior_that_refuses_to_cancel_does_not_hold_the_stop(monkeypatch):
    """Safety waits, but never forever."""
    monkeypatch.setattr("asimoov.core.safety.CANCEL_TIMEOUT_S", 0.05)
    body = FakeBody()

    async def never_returns(reason: str) -> None:
        await asyncio.sleep(10)

    guard = SafetyGuard(body, on_stop=never_returns)
    await guard.stop_all("watchdog")
    assert body.last_stop_all_reason == "watchdog"
