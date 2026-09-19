"""The audio loop the runtime owns: device choice, mic gating, barge-in.

The review's blocker 2 was that nothing in the process ever opened a
microphone: `MicGate`, `BargeInController` and the devices existed but only
the tests used them. These tests drive the loop the runtime now runs.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from asimoov.contracts.fakes import FakeAudioSink, FakeAudioSource, FakeBody
from asimoov.core.voice_loop import AudioError, VoiceLoop, open_audio, plan_audio

SPEECH = (b"\x40\x30" * 480)
SILENCE = b"\x00\x00" * 480


class SilentBody(FakeBody):
    """A body that offers no audio device of its own (the avatar)."""

    def audio_source(self):
        return None

    def audio_sink(self):
        return None


class RecordingProvider:
    """The slice of the provider the loop and the barge-in controller use."""

    sample_rate_hz = 24000

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.active_response_id: str | None = "resp_1"
        self.current_item_id: str | None = "item_1"
        self.has_pending_tool_call = False
        self.canceled: list[str] = []
        self.truncated: list[tuple[str, float]] = []

    async def send_audio(self, pcm16: bytes) -> None:
        self.sent.append(pcm16)

    async def cancel_response(self, response_id: str) -> None:
        self.canceled.append(response_id)

    async def truncate_item(self, item_id: str, played_ms: float) -> None:
        self.truncated.append((item_id, played_ms))


class AlwaysFires:
    name = "always"

    def feed(self, pcm16: bytes) -> bool:
        return True

    def reset(self) -> None:
        return None


def build_loop(**kwargs) -> tuple[VoiceLoop, RecordingProvider, FakeAudioSink]:
    provider = RecordingProvider()
    sink = FakeAudioSink()
    loop = VoiceLoop(provider, FakeAudioSource(chunks=[SPEECH]), sink, **kwargs)
    return loop, provider, sink


def test_a_body_with_its_own_devices_wins():
    plan = plan_audio({}, FakeBody())
    assert plan.ready
    assert plan.source == "body"


def test_a_robot_with_no_backend_at_all_is_not_ready(monkeypatch):
    """`asimoov doctor` must refuse to call this robot ready."""
    monkeypatch.setattr("asimoov.core.voice_loop._desktop_available", lambda: False)
    monkeypatch.setattr("asimoov.core.voice_loop._android_available", lambda: False)
    plan = plan_audio({}, SilentBody())
    assert not plan.ready
    assert "no audio backend" in plan.describe()
    with pytest.raises(AudioError):
        open_audio(plan, {}, SilentBody(), sample_rate_hz=24000)


def test_audio_can_be_turned_off_on_purpose():
    plan = plan_audio({"backend": "none"}, FakeBody())
    assert not plan.ready
    assert "disabled" in plan.describe()


def test_an_unknown_backend_is_refused():
    with pytest.raises(AudioError, match="unknown audio backend"):
        plan_audio({"backend": "telepathy"}, FakeBody())


def test_the_body_backend_is_not_silently_replaced():
    """Asking for the body's devices and not getting them is an error."""
    plan = plan_audio({"backend": "body"}, SilentBody())
    assert not plan.ready
    assert "the body offers no audio device" in plan.describe()


async def test_the_microphone_is_forwarded_when_nothing_is_playing():
    loop, provider, _sink = build_loop()
    await loop.handle_chunk(SPEECH)
    assert provider.sent == [SPEECH]


async def test_not_one_chunk_is_forwarded_while_the_speaker_is_audible():
    """Neon's echo bug: the mic must stay shut until the real head is idle."""
    loop, provider, sink = build_loop()
    await sink.play("item_1", SPEECH * 20)
    assert loop.tracker.is_playing()

    for _ in range(5):
        await loop.handle_chunk(SILENCE)
    assert provider.sent == []


async def test_speech_during_playback_interrupts_instead_of_being_sent():
    from asimoov.voice.barge_in import BargeInDetector

    loop, provider, sink = build_loop()
    loop.barge_in._detector = BargeInDetector(vad=AlwaysFires())
    await sink.play("item_1", SPEECH * 20)

    await loop.handle_chunk(SPEECH)

    assert provider.sent == []
    assert provider.canceled == ["resp_1"]
    assert provider.truncated and provider.truncated[0][0] == "item_1"


async def test_a_renewal_repoints_the_loop_at_the_live_provider():
    loop, _old, _sink = build_loop()
    fresh = RecordingProvider()
    loop.set_provider(fresh)
    await loop.handle_chunk(SPEECH)
    assert fresh.sent == [SPEECH]


async def test_the_loop_is_silent_only_when_nothing_is_streaming():
    loop, provider, sink = build_loop()
    assert not loop.is_silent()  # a response is active
    provider.active_response_id = None
    assert loop.is_silent()
    await sink.play("item_1", SPEECH * 20)
    assert not loop.is_silent()


async def test_starting_the_loop_wires_the_capture_device():
    loop, provider, _sink = build_loop()
    await loop.start()
    try:
        loop.source.emit_chunks()
        for _ in range(20):
            if provider.sent:
                break
            await _yield()
        assert provider.sent == [SPEECH]
    finally:
        await loop.stop()


async def _yield() -> None:
    import asyncio

    await asyncio.sleep(0.01)


def test_default_devices_resolve_to_none():
    from asimoov.core.voice_loop import _device

    assert _device({"source": "default"}, "source") is None
    assert _device({}, "sink") is None
    assert _device({"sink": 3}, "sink") == 3


def test_an_audio_plan_describes_itself():
    plan = replace(plan_audio({}, FakeBody()), reason="because")
    assert plan.describe() == "in=body out=body (because)"


def test_importing_the_core_costs_no_native_library():
    """The runtime reaches into `voice/`; it must not drag onnxruntime in.

    `onnxruntime` is an optional extra and ~100 MB of native libraries. A
    `pip install asimoov` must not import it, and neither must a machine
    that happens to have the `[vad]` extra installed.
    """
    import subprocess
    import sys

    probe = (
        "import sys, asimoov.core.runtime, asimoov.__main__;"
        "forbidden={'pydantic','onnxruntime','cv2','scipy','aiohttp',"
        "'msgspec','orjson','uvloop','torch','sounddevice','jnius'};"
        "print(sorted(forbidden & set(sys.modules)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"
