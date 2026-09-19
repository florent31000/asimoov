"""Smoke tests for the shared fakes other than FakeBody (covered by conformance)."""

from __future__ import annotations

import pytest

from asimoov.contracts.face import FaceState
from asimoov.contracts.fakes import (
    FakeAudioSink,
    FakeAudioSource,
    FakeFaceRenderer,
    FakePlaybackTracker,
    FakeVoiceProvider,
    InMemoryMemoryStore,
)
from asimoov.contracts.memory import Fact, Person


@pytest.mark.asyncio
async def test_fake_voice_provider_replays_script() -> None:
    seen: list[str] = []

    class Events:
        def on_utterance(self, utterance):
            seen.append(utterance)

    provider = FakeVoiceProvider(script=[("on_utterance", ("bonjour",))])
    await provider.start(Events(), {})
    await provider.play_script()
    assert seen == ["bonjour"]

    with pytest.raises(ValueError):
        await provider.cancel_response("")


@pytest.mark.asyncio
async def test_fake_face_renderer_records_states() -> None:
    renderer = FakeFaceRenderer()
    await renderer.start({})
    await renderer.render(FaceState(emotion="happy"))
    await renderer.stop()
    assert renderer.started and renderer.stopped
    assert [s.emotion for s in renderer.states] == ["happy"]


@pytest.mark.asyncio
async def test_in_memory_memory_store_recall() -> None:
    store = InMemoryMemoryStore()
    await store.upsert_person(Person(id="p1", name="Sam"))
    await store.add_fact(Fact(person_id="p1", text="loves dinosaurs"))

    assert await store.find_person_by_name("sam") is not None
    assert await store.recall("dinosaurs") == ["loves dinosaurs"]
    assert await store.recall("unrelated query") == []
    assert await store.match_face(object()) is None


@pytest.mark.asyncio
async def test_fake_audio_source_emits_silence() -> None:
    source = FakeAudioSource(sample_rate_hz=16000)
    chunks: list[bytes] = []
    await source.start(chunks.append)

    source.emit_silence(count=3, chunk_ms=20.0)

    assert len(chunks) == 3
    expected_len = int(16000 / 1000 * 2 * 1 * 20.0)  # sample_rate/1000 * width * channels * ms
    assert all(len(chunk) == expected_len for chunk in chunks)
    assert all(chunk == b"\x00" * expected_len for chunk in chunks)


@pytest.mark.asyncio
async def test_fake_audio_source_emits_scripted_chunks() -> None:
    source = FakeAudioSource(chunks=[b"\x01\x02", b"\x03\x04"])
    chunks: list[bytes] = []
    await source.start(chunks.append)

    source.emit_chunks()

    assert chunks == [b"\x01\x02", b"\x03\x04"]


def test_fake_audio_source_emit_before_start_raises() -> None:
    source = FakeAudioSource()
    with pytest.raises(RuntimeError):
        source.emit_silence()


@pytest.mark.asyncio
async def test_fake_audio_sink_records_writes_and_drives_tracker() -> None:
    sink = FakeAudioSink(sample_rate_hz=16000)
    bytes_per_ms = 16000 / 1000 * 2  # PCM16 mono
    pcm16 = b"\x00" * int(bytes_per_ms * 100)  # 100 ms of audio

    await sink.play("item-1", pcm16)

    assert sink.written == [("item-1", pcm16)]
    tracker = sink.tracker()
    assert tracker.is_playing()
    assert tracker.remaining_ms() == pytest.approx(100.0)

    tracker.advance_ms(40)
    assert tracker.played_ms("item-1") == pytest.approx(40.0)
    assert tracker.remaining_ms() == pytest.approx(60.0)
    assert tracker.is_playing()

    tracker.advance_ms(60)
    assert tracker.played_ms("item-1") == pytest.approx(100.0)
    assert not tracker.is_playing()
    assert tracker.energy_at_head() == 0.0


@pytest.mark.asyncio
async def test_fake_audio_sink_stop_flushes_tracker() -> None:
    sink = FakeAudioSink(sample_rate_hz=16000)
    await sink.play("item-1", b"\x00" * 3200)  # 100 ms

    await sink.stop()

    assert not sink.tracker().is_playing()
    assert sink.tracker().remaining_ms() == 0.0


def test_fake_playback_tracker_flush_discards_queue() -> None:
    tracker = FakePlaybackTracker()
    tracker._enqueue("a", 100.0)
    tracker._enqueue("b", 50.0)

    tracker.flush()

    assert tracker.remaining_ms() == 0.0
    assert not tracker.is_playing()
    assert tracker.played_ms("a") == 0.0
