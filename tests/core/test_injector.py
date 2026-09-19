"""Injector: coalescing, dedup, budget, never during an active response."""

from __future__ import annotations

from asimoov.core.bus.local import LocalBus
from asimoov.core.clock import ManualClock
from asimoov.core.mind.injector import Injector


def build(**kwargs):
    clock = ManualClock(1000.0)
    sent: list[str] = []

    async def deliver(text: str) -> None:
        sent.append(text)

    return Injector(deliver, clock=clock, **kwargs), clock, sent


async def test_submissions_are_coalesced_into_one_injection():
    injector, clock, sent = build(coalesce_s=1.0)
    injector.submit("Sam just arrived.")
    clock.advance(0.2)
    injector.submit("Sam is smiling.")
    assert await injector.pump() is None

    clock.advance(1.0)
    assert await injector.pump() == "Sam just arrived. Sam is smiling."
    assert sent == ["Sam just arrived. Sam is smiling."]


async def test_a_duplicate_within_the_window_is_dropped():
    injector, clock, _ = build(dedup_s=60.0)
    assert injector.submit("Sam just arrived.") is True
    clock.advance(10)
    assert injector.submit("Sam just arrived.") is False
    assert injector.dropped_duplicates == 1

    clock.advance(61)
    assert injector.submit("Sam just arrived.") is True


async def test_nothing_is_injected_during_an_active_response():
    injector, clock, sent = build()
    injector.set_response_active(True)
    injector.submit("Someone arrived.")
    clock.advance(5)
    assert await injector.pump() is None
    assert injector.pending == ("Someone arrived.",)

    injector.set_response_active(False)
    assert await injector.pump() == "Someone arrived."


async def test_the_budget_holds_the_queue():
    injector, clock, sent = build(budget_per_min=2, coalesce_s=0.0, dedup_s=0.0)
    for index in range(3):
        injector.submit(f"note {index}")
        clock.advance(1)
        await injector.pump()
    assert len(sent) == 2
    assert injector.pending == ("note 2",)

    clock.advance(61)
    assert await injector.pump() == "note 2"


async def test_the_text_is_truncated():
    injector, clock, _ = build(max_chars=20, coalesce_s=0.0)
    injector.submit("x" * 50)
    clock.advance(1)
    assert len(await injector.pump()) == 20


async def test_the_injection_is_published_on_the_bus():
    bus = LocalBus()
    seen: list[str] = []

    async def handler(envelope):
        seen.append(envelope.data["text"])

    bus.subscribe("mind.injection", handler)
    clock = ManualClock(0.0)

    async def deliver(text: str) -> None:
        return None

    injector = Injector(deliver, bus=bus, clock=clock, coalesce_s=0.0)
    injector.submit("hello")
    clock.advance(1)
    await injector.pump()
    assert seen == ["hello"]


async def test_empty_text_is_ignored():
    injector, _, _ = build()
    assert injector.submit("   ") is False
    assert injector.pending == ()
