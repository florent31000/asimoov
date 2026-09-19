"""Barge-in: detection, and the interrupt sequence against the fake server."""

from __future__ import annotations

import numpy as np
import pytest
from conftest import pcm_silence
from fake_realtime_server import wait_until

from asimoov.contracts.fakes import FakeAudioSink
from asimoov.voice import vad_silero
from asimoov.voice.audio.tracker import MicGate
from asimoov.voice.barge_in import (
    BargeInConfig,
    BargeInController,
    BargeInDetector,
    RelativeEnergyVad,
)

RATE = 24000


def tone(ms: float, amplitude: int) -> bytes:
    samples = int(RATE / 1000 * ms)
    time_axis = np.arange(samples, dtype=np.float32)
    wave = np.sin(2 * np.pi * 220 * time_axis / RATE) * amplitude
    return wave.astype(np.int16).tobytes()


def test_relative_energy_needs_speech_above_the_floor_for_300_ms():
    vad = RelativeEnergyVad(sample_rate_hz=RATE, k=3.0, min_speech_ms=300.0)
    for _ in range(20):
        assert vad.feed(tone(20, 200)) is False  # room tone builds the floor

    detections = [vad.feed(tone(20, 8000)) for _ in range(15)]
    assert detections[:14].count(True) == 0
    assert detections[14] is True


def test_short_bursts_do_not_trigger():
    vad = RelativeEnergyVad(sample_rate_hz=RATE, k=3.0, min_speech_ms=300.0)
    for _ in range(20):
        vad.feed(tone(20, 200))
    for _ in range(5):  # 100 ms of noise
        assert vad.feed(tone(20, 8000)) is False
    assert vad.feed(tone(20, 200)) is False
    for _ in range(5):
        assert vad.feed(tone(20, 8000)) is False


def test_capabilities_report_the_fallback():
    detector = BargeInDetector(config=BargeInConfig(enabled=False))
    assert detector.capabilities() == {
        "mode": "half_duplex",
        "vad": "none",
        "min_speech_ms": 300.0,
        "tail_ms": 250.0,
        "reason": "disabled by configuration",
    }
    assert detector.feed(tone(20, 30000)) is False

    energy = BargeInDetector(vad=RelativeEnergyVad())
    assert energy.capabilities()["mode"] == "full_duplex"
    assert energy.capabilities()["vad"] == "energy"


class AlwaysDetects:
    name = "stub"

    def __init__(self) -> None:
        self.resets = 0

    def feed(self, pcm16: bytes) -> bool:
        return True

    def reset(self) -> None:
        self.resets += 1


async def _playing_provider(server, start_provider, events):
    provider = await start_provider()
    connection = await server.connection()
    sink = FakeAudioSink()
    tracker = sink.tracker()
    await sink.play("item_1", pcm_silence(1000))
    tracker.advance_ms(320)

    await connection.emit_response_created("resp_x")
    await connection.emit_audio("item_1", pcm_silence(20))
    await wait_until(lambda: provider.current_item_id == "item_1")
    await wait_until(lambda: provider.active_response_id == "resp_x")
    return provider, connection, tracker


async def test_barge_in_flushes_cancels_with_the_id_and_truncates(
    server, events, start_provider
):
    provider, connection, tracker = await _playing_provider(server, start_provider, events)
    now = [0.0]
    gate = MicGate(tracker, tail_ms=250, clock=lambda: now[0])
    assert gate.is_open() is False

    controller = BargeInController(
        provider,
        tracker,
        detector=BargeInDetector(vad=AlwaysDetects()),
        gate=gate,
    )
    assert await controller.feed(tone(20, 20000)) is True

    await connection.wait_for("response.cancel")
    await connection.wait_for("conversation.item.truncate")
    assert connection.canceled[0]["response_id"] == "resp_x"
    assert connection.truncations[0]["item_id"] == "item_1"
    assert connection.truncations[0]["audio_end_ms"] == 320
    assert tracker.remaining_ms() == 0
    assert gate.is_open() is True


async def test_barge_in_keeps_the_response_while_a_tool_call_runs(
    server, events, start_provider
):
    provider, connection, tracker = await _playing_provider(server, start_provider, events)
    await connection.emit_tool_call("gesture", {"name": "wave"}, "call_1")
    await wait_until(lambda: provider.has_pending_tool_call)

    controller = BargeInController(
        provider, tracker, detector=BargeInDetector(vad=AlwaysDetects())
    )
    assert await controller.trigger() is True

    await connection.wait_for("conversation.item.truncate")
    assert connection.canceled == []
    assert connection.truncations[0]["audio_end_ms"] == 320


async def test_nothing_happens_when_no_audio_is_playing(server, events, start_provider):
    provider = await start_provider()
    connection = await server.connection()
    sink = FakeAudioSink()
    detector = BargeInDetector(vad=AlwaysDetects())
    controller = BargeInController(provider, sink.tracker(), detector=detector)

    assert await controller.feed(tone(20, 30000)) is False
    assert connection.canceled == []
    assert connection.truncations == []


def test_silero_runs_when_the_model_is_installed():
    if not vad_silero.available():
        pytest.skip("no Silero model on this host")
    vad = vad_silero.SileroVad(sample_rate_hz=RATE)

    silence = np.zeros(int(RATE * 0.4), dtype=np.int16).tobytes()
    assert vad.feed(silence) is False  # runs the ONNX graph on 6 frames
    vad.reset()
    assert vad.feed(silence) is False
