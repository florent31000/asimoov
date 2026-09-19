from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# pytest runs with --import-mode=importlib, which does not put the test
# directory on sys.path: do it here so fake_realtime_server imports everywhere.
sys.path.insert(0, str(Path(__file__).parent))

from fake_realtime_server import FakeRealtimeServer  # noqa: E402

from asimoov.contracts.percepts import Utterance
from asimoov.contracts.tools import ToolSpec
from asimoov.voice.openai_realtime import OpenAIRealtimeProvider

TOOLS = (
    ToolSpec(
        name="gesture",
        description="Play a gesture.",
        params={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    ),
)

BASE_CONFIG: dict[str, Any] = {
    "model": "gpt-realtime-1.5",
    "voice": "alloy",
    "instructions": "You are a robot.",
    "transcription_language": "fr",
    "tools": TOOLS,
}


class RecordingEvents:
    """`VoiceEvents` implementation that records every callback."""

    def __init__(self) -> None:
        self.speech_started = 0
        self.speech_ended = 0
        self.utterances: list[Utterance] = []
        self.tool_calls: list[tuple[str, dict[str, Any], str]] = []
        self.audio: list[tuple[str, bytes]] = []
        self.responses_started: list[str] = []
        self.responses_done: list[str] = []

    def on_speech_started(self) -> None:
        self.speech_started += 1

    def on_speech_ended(self) -> None:
        self.speech_ended += 1

    def on_utterance(self, utterance: Utterance) -> None:
        self.utterances.append(utterance)

    def on_tool_call(self, name: str, params: dict[str, Any], call_id: str) -> None:
        self.tool_calls.append((name, params, call_id))

    def on_audio_out(self, item_id: str, pcm16: bytes) -> None:
        self.audio.append((item_id, pcm16))

    def on_response_started(self, response_id: str) -> None:
        self.responses_started.append(response_id)

    def on_response_done(self, response_id: str) -> None:
        self.responses_done.append(response_id)


@pytest.fixture
async def server():
    instance = FakeRealtimeServer()
    await instance.start()
    yield instance
    await instance.stop()


@pytest.fixture
def events() -> RecordingEvents:
    return RecordingEvents()


@pytest.fixture
async def start_provider(server, events):
    """Factory starting a provider against the fake server, ready to use."""
    started: list[OpenAIRealtimeProvider] = []

    async def _start(config: dict[str, Any] | None = None, **kwargs: Any):
        provider = OpenAIRealtimeProvider(api_key="", url=server.url, **kwargs)
        await provider.start(events, {**BASE_CONFIG, **(config or {})})
        await provider.wait_ready(2.0)
        started.append(provider)
        return provider

    yield _start
    for provider in started:
        await provider.stop()


def pcm_silence(ms: float, sample_rate_hz: int = 24000) -> bytes:
    return b"\x00" * int(sample_rate_hz / 1000 * 2 * ms)
