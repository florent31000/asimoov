"""The family-evening replay, end to end through a real runtime."""

from __future__ import annotations

import json

import pytest
from tests.core.conftest import REPLAY_DIR

from asimoov.contracts.fakes import FakeBody, FakeVoiceProvider
from asimoov.core.clock import ScaledClock
from asimoov.core.memory.sqlite_store import SqliteMemoryStore
from asimoov.core.replay import (
    ReplayAssertionError,
    Replayer,
    ReplayError,
    first_timestamp,
)
from asimoov.core.runtime import Runtime

FAMILY_EVENING = REPLAY_DIR / "family_evening.jsonl"
SPEED = 30.0


async def run_replay(config, path, tmp_path, *, speed=SPEED, max_gap_s=0.3):
    clock = ScaledClock(origin=first_timestamp(path) or 0.0, speed=speed)
    runtime = Runtime(
        config=config,
        body=FakeBody(),
        voice=FakeVoiceProvider(),
        memory=SqliteMemoryStore(tmp_path / "memory.db"),
        clock=clock,
        tick_s=1.0 / speed,
        hub_enabled=False,
    )
    await runtime.start()
    clock.reset()
    try:
        return await Replayer(
            runtime.bus, speed=speed, clock=clock, max_gap_s=max_gap_s
        ).run(path), runtime
    finally:
        await runtime.stop()


def test_the_fixture_is_valid_jsonl():
    lines = [
        json.loads(line)
        for line in FAMILY_EVENING.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    envelopes = [line for line in lines if "assert" not in line]
    assertions = [line for line in lines if "assert" in line]
    assert len(envelopes) == 13
    assert len(assertions) == 5
    assert all(envelope["v"] == 1 for envelope in envelopes)


def test_first_timestamp_skips_comments_and_assertions():
    assert first_timestamp(FAMILY_EVENING) == 1758290400.0


async def test_sam_arrives_is_remembered_and_recognized(avatar_config, tmp_path):
    result, runtime = await run_replay(avatar_config, FAMILY_EVENING, tmp_path)
    assert result.envelopes == 13
    assert result.assertions == 5

    store = runtime.memory
    await store.open()
    try:
        sam = await store.get_person("person:sam")
        assert sam is not None and sam.name == "Sam"
        assert await store.recall("InMoov")
    finally:
        await store.close()


async def test_a_failing_assertion_names_what_was_missing(avatar_config, tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text(
        json.dumps(
            {
                "v": 1,
                "kind": "percept",
                "topic": "percept.battery",
                "id": "x",
                "ts": 1000.0,
                "src": "test",
                "data": {"type": "battery", "level": 0.5},
            }
        )
        + "\n"
        + json.dumps({"assert": "expect", "topic": "mind.injection", "contains": "nope", "within_s": 0.2})
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ReplayAssertionError, match="mind.injection"):
        await run_replay(avatar_config, path, tmp_path, speed=1.0)


async def test_a_malformed_line_is_reported(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text("{not json}\n", encoding="utf-8")
    from asimoov.core.bus.local import LocalBus

    with pytest.raises(ReplayError, match="invalid JSON"):
        await Replayer(LocalBus()).run(path)


async def test_an_unknown_assertion_is_reported(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps({"assert": "hope"}) + "\n", encoding="utf-8")
    from asimoov.core.bus.local import LocalBus

    with pytest.raises(ReplayError, match="unknown assertion"):
        await Replayer(LocalBus()).run(path)


async def test_a_missing_file_is_reported(tmp_path):
    from asimoov.core.bus.local import LocalBus

    with pytest.raises(ReplayError, match="no such replay file"):
        await Replayer(LocalBus()).run(tmp_path / "nope.jsonl")
