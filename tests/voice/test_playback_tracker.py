"""Playback head arithmetic and the mic gate built on it."""

from __future__ import annotations

from asimoov.voice.audio.tracker import MicGate, StreamPlaybackTracker

RATE = 24000


def pcm(ms: float, level: int = 0) -> bytes:
    samples = int(RATE / 1000 * ms)
    return (level.to_bytes(2, "little", signed=True)) * samples


def test_remaining_ms_counts_audio_not_yet_consumed():
    tracker = StreamPlaybackTracker(RATE)
    tracker.feed("item_1", pcm(100))

    assert tracker.remaining_ms() == 100
    assert tracker.is_playing() is True

    tracker.read(int(RATE * 0.02))
    assert tracker.remaining_ms() == 80
    assert tracker.played_ms("item_1") == 20


def test_hardware_latency_keeps_the_head_behind_the_callback():
    tracker = StreamPlaybackTracker(RATE, output_latency_ms=50)
    tracker.feed("item_1", pcm(100))
    tracker.read(int(RATE * 0.1))  # the callback consumed everything

    # 50 ms are still in the hardware buffer: Neon reopened the mic here.
    assert tracker.remaining_ms() == 50
    assert tracker.is_playing() is True
    assert tracker.played_ms("item_1") == 50

    tracker.read(int(RATE * 0.05))
    assert tracker.remaining_ms() == 0
    assert tracker.is_playing() is False
    assert tracker.played_ms("item_1") == 100


def test_played_ms_is_per_item_and_survives_flush():
    tracker = StreamPlaybackTracker(RATE)
    tracker.feed("item_1", pcm(100))
    tracker.feed("item_2", pcm(100))
    tracker.read(int(RATE * 0.15))

    assert tracker.played_ms("item_1") == 100
    assert tracker.played_ms("item_2") == 50

    tracker.flush()
    assert tracker.remaining_ms() == 0
    assert tracker.played_ms("item_2") == 50


def test_energy_at_head_follows_the_audible_block():
    tracker = StreamPlaybackTracker(RATE, output_latency_ms=20)
    tracker.feed("item_1", pcm(20, level=0))
    tracker.feed("item_1", pcm(20, level=20000))

    tracker.read(int(RATE * 0.02))
    assert tracker.energy_at_head() == 0.0  # still playing the silent block

    tracker.read(int(RATE * 0.02))
    assert tracker.energy_at_head() > 0.5

    tracker.read(int(RATE * 0.02))
    assert tracker.energy_at_head() == 0.0


def test_energy_callback_receives_every_block():
    tracker = StreamPlaybackTracker(RATE, output_latency_ms=20)
    seen: list[float] = []
    tracker.set_energy_callback(seen.append)
    tracker.feed("item_1", pcm(20, level=0))
    tracker.feed("item_1", pcm(20, level=20000))

    tracker.read(int(RATE * 0.02))
    tracker.read(int(RATE * 0.02))
    assert len(seen) == 2
    assert seen[0] == 0.0
    assert 0.5 < seen[1] <= 1.0


def test_mic_gate_stays_closed_for_the_tail():
    now = [0.0]
    tracker = StreamPlaybackTracker(RATE)
    gate = MicGate(tracker, tail_ms=250, clock=lambda: now[0])

    assert gate.is_open() is True
    tracker.feed("item_1", pcm(100))
    assert gate.is_open() is False

    tracker.read(int(RATE * 0.1))
    assert gate.is_open() is False  # tail not elapsed
    now[0] += 0.2
    assert gate.is_open() is False
    now[0] += 0.06
    assert gate.is_open() is True


def test_ungate_opens_the_gate_immediately():
    now = [0.0]
    tracker = StreamPlaybackTracker(RATE)
    gate = MicGate(tracker, tail_ms=250, clock=lambda: now[0])
    tracker.feed("item_1", pcm(100))
    assert gate.is_open() is False

    tracker.flush()
    gate.ungate()
    assert gate.is_open() is True


def test_first_audio_played_ts_fires_once_per_item():
    tracker = StreamPlaybackTracker(RATE)
    stamps: list[tuple[str, float]] = []
    tracker.set_timestamp_callback(lambda name, ts: stamps.append((name, ts)))

    tracker.feed("item_1", pcm(40))
    tracker.read(int(RATE * 0.02))
    tracker.read(int(RATE * 0.02))
    assert [name for name, _ in stamps] == ["first_audio_played_ts"]

    tracker.feed("item_2", pcm(20))
    tracker.read(int(RATE * 0.02))
    assert len(stamps) == 2
