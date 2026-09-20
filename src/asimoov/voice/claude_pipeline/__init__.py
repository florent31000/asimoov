"""Claude pipeline: local VAD -> STT -> Claude Messages API -> TTS.

The Claude Messages API takes text and images only, so a companion robot
speaking through it needs the audio ends supplied locally. This package is
those ends plus the turn on top, behind the frozen `VoiceProvider` ABC.
"""

from asimoov.voice.claude_pipeline.provider import ClaudePipelineProvider, tool_to_claude
from asimoov.voice.claude_pipeline.stt import (
    FakeSTT,
    FasterWhisperSTT,
    SpeechToText,
    build_stt,
)
from asimoov.voice.claude_pipeline.tts import FakeTTS, KokoroTTS, TextToSpeech, build_tts
from asimoov.voice.claude_pipeline.turns import TurnConfig, TurnDetector

__all__ = [
    "ClaudePipelineProvider",
    "FakeSTT",
    "FakeTTS",
    "FasterWhisperSTT",
    "KokoroTTS",
    "SpeechToText",
    "TextToSpeech",
    "TurnConfig",
    "TurnDetector",
    "build_stt",
    "build_tts",
    "tool_to_claude",
]
