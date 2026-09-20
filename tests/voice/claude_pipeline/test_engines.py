"""Turn detection and the STT/TTS engine factories."""

from __future__ import annotations

import pytest

from asimoov.voice.claude_pipeline.stt import FakeSTT, FasterWhisperSTT, build_stt
from asimoov.voice.claude_pipeline.tts import (
    KOKORO_DEFAULTS,
    FakeTTS,
    KokoroTTS,
    build_tts,
)
from asimoov.voice.claude_pipeline.turns import (
    SPEECH_ENDED,
    SPEECH_STARTED,
    TurnConfig,
    TurnDetector,
)

CHUNK_MS = 10.0
LOUD = b"\x11\x22" * 240
SILENCE = b"\x00\x00" * 240


class LoudVad:
    name = "test"

    def feed(self, pcm16: bytes) -> bool:
        return bool(pcm16) and max(pcm16) > 8

    def reset(self) -> None:
        return None


def detector(**kwargs) -> TurnDetector:
    return TurnDetector(config=TurnConfig(**kwargs), vad=LoudVad())


def test_a_short_noise_never_opens_a_turn():
    turns = detector(min_speech_ms=300.0, end_silence_ms=700.0)
    assert [turns.feed(LOUD) for _ in range(29)] == [None] * 29
    assert not turns.speaking


def test_the_turn_opens_after_min_speech_and_closes_after_the_silence():
    turns = detector(min_speech_ms=300.0, end_silence_ms=700.0)
    transitions = [turns.feed(LOUD) for _ in range(30)]
    assert transitions[-1] == SPEECH_STARTED
    assert turns.speaking

    silent = [turns.feed(SILENCE) for _ in range(70)]
    assert silent[:-1] == [None] * 69
    assert silent[-1] == SPEECH_ENDED
    assert not turns.speaking


def test_a_pause_inside_a_sentence_does_not_end_the_turn():
    turns = detector(min_speech_ms=0.0, end_silence_ms=700.0)
    assert turns.feed(LOUD) == SPEECH_STARTED
    assert [turns.feed(SILENCE) for _ in range(40)] == [None] * 40
    assert turns.feed(LOUD) is None
    assert [turns.feed(SILENCE) for _ in range(69)] == [None] * 69
    assert turns.feed(SILENCE) == SPEECH_ENDED


def test_the_detector_says_which_vad_it_runs():
    """`asimoov doctor` prints this; without Silero it must say so."""
    turns = TurnDetector(sample_rate_hz=24000)
    capabilities = turns.capabilities()
    assert capabilities["vad"] in ("silero", "energy")
    if capabilities["vad"] == "energy":
        assert capabilities["reason"] == "silero model or onnxruntime missing"


async def test_the_fake_engines_are_deterministic():
    stt = FakeSTT(["un", "deux"])
    assert await stt.transcribe(b"\x00\x00", sample_rate_hz=24000) == "un"
    assert await stt.transcribe(b"\x00\x00", sample_rate_hz=24000) == "deux"
    assert await stt.transcribe(b"\x00\x00", sample_rate_hz=24000) == "deux"

    tts = FakeTTS(ms_per_char=10.0)
    short = await tts.synthesize("abc", sample_rate_hz=24000)
    long = await tts.synthesize("abcdef", sample_rate_hz=24000)
    assert len(long) == 2 * len(short)
    assert await tts.synthesize("   ", sample_rate_hz=24000) == b""


def test_the_engine_factories_refuse_a_typo():
    assert build_stt({}).name == "faster_whisper"
    assert build_tts({}, language="en").name == "kokoro"
    with pytest.raises(ValueError, match="unknown stt engine"):
        build_stt({"engine": "wisper"})
    with pytest.raises(ValueError, match="unknown tts engine"):
        build_tts({"engine": "elevenlabs"}, language="fr")


def test_the_french_default_voice_is_kokoros_only_french_voice():
    """Kokoro-82M ships exactly one French voice; see docs/voice.md."""
    assert KOKORO_DEFAULTS["fr"] == ("fr-fr", "ff_siwis")
    french = build_tts({}, language="fr")
    assert (french.voice, french.lang) == ("ff_siwis", "fr-fr")
    assert build_tts({}, language="en").voice == "af_heart"


def test_a_language_without_a_default_voice_must_name_one():
    with pytest.raises(ValueError, match="no default Kokoro voice"):
        build_tts({}, language="de")
    assert build_tts({"voice": "df_x"}, language="de").lang == "de"


def test_availability_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("ASIMOOV_HOME", str(tmp_path))
    ok, reason = FasterWhisperSTT().available()
    assert isinstance(ok, bool) and reason
    lang, voice = KOKORO_DEFAULTS["en"]
    ok, reason = KokoroTTS(lang=lang, voice=voice).available()
    assert isinstance(ok, bool) and reason
