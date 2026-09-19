"""Session thresholds, light truncation, and hot renewal without an audio gap."""

from __future__ import annotations

import asyncio

from conftest import BASE_CONFIG
from fake_realtime_server import wait_until

from asimoov.voice.openai_realtime import OpenAIRealtimeProvider
from asimoov.voice.session import SessionLimits, SessionManager


def make_manager(server, events, **kwargs) -> SessionManager:
    def factory() -> OpenAIRealtimeProvider:
        return OpenAIRealtimeProvider(api_key="", url=server.url)

    return SessionManager(factory, dict(BASE_CONFIG), events, **kwargs)


async def test_thresholds(server, events):
    now = [0.0]
    manager = make_manager(server, events, clock=lambda: now[0])
    await manager.start()

    assert manager.renewal_reason() is None
    assert manager.is_idle() is False

    manager.note_usage({"total_tokens": 24_000})
    assert manager.renewal_reason() is None
    manager.note_usage({"total_tokens": 24_001})
    assert manager.renewal_reason() == "tokens"

    manager.note_usage({"total_tokens": 0})  # usage is a high-water mark
    assert manager.total_tokens == 24_001
    await manager.stop()

    aged = make_manager(server, events, clock=lambda: now[0])
    await aged.start()
    now[0] += SessionLimits().max_age_s + 1
    assert aged.renewal_reason() == "age"

    aged.note_activity()
    assert aged.is_idle() is False
    now[0] += SessionLimits().idle_timeout_s + 1
    assert aged.is_idle() is True
    await aged.stop()


async def test_renewal_switches_before_closing_the_old_session(server, events):
    summaries: list[str] = []
    renewals: list[str] = []
    manager = make_manager(server, events, on_summary=summaries.append, on_renewal=renewals.append)
    await manager.start()
    old = await server.connection(0)

    await old.emit_audio("item_1", b"\x00\x00" * 240)
    await wait_until(lambda: len(events.audio) == 1)

    manager.note_usage({"total_tokens": 30_000})
    await manager.tick()
    new = await server.connection(1)

    assert manager.renewals == 1
    assert renewals == ["tokens"]

    requests = [e for e in old.received if e["type"] == "response.create"]
    assert requests[0]["response"]["conversation"] == "none"
    assert requests[0]["response"]["output_modalities"] == ["text"]
    assert summaries == [old.summary_text]
    assert old.summary_text in new.session["instructions"]
    assert BASE_CONFIG["instructions"] in new.session["instructions"]

    await wait_until(lambda: old.closed_seq is not None)
    assert new.session_updated_seq < old.closed_seq

    await new.emit_audio("item_2", b"\x00\x00" * 240)
    await wait_until(lambda: len(events.audio) == 2)
    assert manager.total_tokens == 0
    await manager.stop()


async def test_switch_waits_for_the_next_silence(server, events):
    silent = [False]
    manager = make_manager(server, events, is_silent=lambda: silent[0])
    await manager.start()
    await server.connection(0)

    task = asyncio.create_task(manager.renew("age"))
    await asyncio.sleep(0.15)
    assert manager.renewals == 0

    silent[0] = True
    await task
    assert manager.renewals == 1
    await manager.stop()


async def test_tick_disconnects_after_the_idle_timeout(server, events):
    now = [0.0]
    manager = make_manager(server, events, clock=lambda: now[0])
    await manager.start()
    await server.connection(0)

    now[0] = SessionLimits().idle_timeout_s + 1
    await manager.tick()
    assert manager.active is None


async def test_tick_truncates_beyond_forty_items(server, events):
    manager = make_manager(server, events)
    provider = await manager.start()
    connection = await server.connection(0)

    await connection.emit_item_created("item_sys", role="system")
    for index in range(44):
        await connection.emit_item_created(f"item_{index}")
    await wait_until(lambda: len(provider.item_ids) == 45)

    await manager.tick()
    await connection.wait_for("conversation.item.delete", count=5)
    assert connection.deleted_items == [f"item_{index}" for index in range(5)]
    await wait_until(lambda: len(provider.item_ids) == 40)
    assert provider.item_ids[0] == "item_sys"
    await manager.stop()
