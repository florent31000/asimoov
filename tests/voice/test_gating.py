"""No microphone chunk reaches the provider while audio is still sounding.

This is the core-side wiring (`docs/voice.md`, "Wiring"): the `MicGate` is the
only thing between the `AudioSource` and `VoiceProvider.send_audio`.
"""

from __future__ import annotations

from conftest import pcm_silence

from asimoov.contracts.fakes import FakeAudioSink
from asimoov.voice.audio.tracker import MicGate


async def test_mic_stays_shut_while_the_tracker_reports_audio(server, start_provider):
    provider = await start_provider()
    connection = await server.connection()

    sink = FakeAudioSink()
    tracker = sink.tracker()
    now = [0.0]
    gate = MicGate(tracker, tail_ms=250, clock=lambda: now[0])

    async def pump(chunks: int) -> None:
        for _ in range(chunks):
            now[0] += 0.02
            tracker.advance_ms(20)
            if gate.is_open():
                await provider.send_audio(pcm_silence(20))

    await sink.play("item_1", pcm_silence(200))
    await pump(10)  # the whole 200 ms of playback
    assert tracker.remaining_ms() == 0
    assert connection.audio_chunks == []

    await pump(11)  # 220 ms of tail: still under the 250 ms gate
    assert connection.audio_chunks == []

    await pump(5)
    await connection.wait_for("input_audio_buffer.append")
    assert len(connection.audio_chunks) >= 1
