from __future__ import annotations

from typing import Any

import pytest

# `fake_anthropic` lives next to `fake_realtime_server` in tests/voice, which
# the parent conftest puts on sys.path; a second conftest.py on sys.path here
# would shadow that one for the sibling suites.
from fake_anthropic import FakeAnthropic, assert_valid_history

from asimoov.contracts.tools import ToolSpec
from asimoov.voice.claude_pipeline import FakeSTT, FakeTTS, TurnConfig, TurnDetector
from asimoov.voice.claude_pipeline.provider import ClaudePipelineProvider

LOUD = b"\x11\x22" * 240
SILENCE = b"\x00\x00" * 240

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
    "model": "claude-opus-5",
    "voice": "af_heart",
    "instructions": "You are a robot.",
    "transcription_language": "fr",
    "tools": TOOLS,
}


class LoudVad:
    """Any chunk that is not digital silence counts as speech."""

    name = "test"

    def feed(self, pcm16: bytes) -> bool:
        return bool(pcm16) and max(pcm16) > 8

    def reset(self) -> None:
        return None


def scripted_turns() -> TurnDetector:
    """A detector that latches on the first loud chunk and ends on the first silent one."""
    return TurnDetector(
        config=TurnConfig(min_speech_ms=0.0, end_silence_ms=1.0), vad=LoudVad()
    )


class RecordingEvents:
    """`VoiceEvents` implementation that records every callback."""

    def __init__(self) -> None:
        self.speech_started = 0
        self.speech_ended = 0
        self.utterances: list[Any] = []
        self.tool_calls: list[tuple[str, dict[str, Any], str]] = []
        self.audio: list[tuple[str, bytes]] = []
        self.responses_started: list[str] = []
        self.responses_done: list[str] = []
        self.assistant_text: list[str] = []

    def on_speech_started(self) -> None:
        self.speech_started += 1

    def on_speech_ended(self) -> None:
        self.speech_ended += 1

    def on_utterance(self, utterance: Any) -> None:
        self.utterances.append(utterance)

    def on_tool_call(self, name: str, params: dict[str, Any], call_id: str) -> None:
        self.tool_calls.append((name, params, call_id))

    def on_audio_out(self, item_id: str, pcm16: bytes) -> None:
        self.audio.append((item_id, pcm16))

    def on_response_started(self, response_id: str) -> None:
        self.responses_started.append(response_id)

    def on_response_done(self, response_id: str) -> None:
        self.responses_done.append(response_id)

    def on_assistant_text(self, text: str) -> None:
        self.assistant_text.append(text)


@pytest.fixture
def events() -> RecordingEvents:
    return RecordingEvents()


@pytest.fixture
async def start_provider(events):
    """Factory starting a provider on a scripted client, ready to use.

    Every request that leaves the provider, and the history it kept, is
    checked against the Messages API placement rules at teardown -- so no
    test in this suite can go green on a conversation the API would reject.
    """
    started: list[tuple[ClaudePipelineProvider, FakeAnthropic]] = []

    async def _start(
        client: FakeAnthropic,
        *,
        transcripts: str | list[str] = "Bonjour.",
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ClaudePipelineProvider:
        provider = ClaudePipelineProvider(
            api_key="unused",
            client=client,
            stt=FakeSTT(transcripts),
            tts=FakeTTS(ms_per_char=1.0),
            turns=scripted_turns(),
            **kwargs,
        )
        await provider.start(events, {**BASE_CONFIG, **(config or {})})
        await provider.wait_ready(2.0)
        started.append((provider, client))
        return provider

    yield _start
    for provider, client in started:
        for request in client.requests:
            assert_valid_history(request["messages"])
        if provider.messages:
            assert_valid_history(provider.messages)
        await provider.stop()
