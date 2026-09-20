"""`Runtime` on top of the real `ClaudePipelineProvider`, with a scripted client.

The joint the Realtime provider exercises in `test_runtime_openai.py` is a
websocket; here it is a manual tool loop over the Messages API, so the same
questions get new answers: does the runtime hand real `ToolSpec`s to a
provider that speaks JSON Schema, does one `tool_use` produce exactly one
continuation, and does `remember_person` still reach the memory store.

The replay is the `family_evening` opening, driven the only way this provider
can be driven: microphone audio into the local turn detector.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

VOICE_TESTS = Path(__file__).resolve().parents[1] / "voice"
if str(VOICE_TESTS) not in sys.path:
    sys.path.insert(0, str(VOICE_TESTS))

from fake_anthropic import FakeAnthropic, text_turn, tool_turn  # noqa: E402
from fake_realtime_server import wait_until  # noqa: E402

from asimoov.contracts.envelope import Envelope  # noqa: E402
from asimoov.contracts.fakes import FakeBody  # noqa: E402
from asimoov.contracts.percepts import PersonSeen  # noqa: E402
from asimoov.contracts.tools import ToolSpec  # noqa: E402
from asimoov.contracts.vocab import TOPICS  # noqa: E402
from asimoov.core.config import load_robot_config  # noqa: E402
from asimoov.core.memory.sqlite_store import SqliteMemoryStore  # noqa: E402
from asimoov.core.runtime import Runtime  # noqa: E402
from asimoov.core.tools.builtin import ENROLL_TOPIC  # noqa: E402
from asimoov.voice.claude_pipeline import FakeSTT, FakeTTS, TurnConfig, TurnDetector  # noqa: E402
from asimoov.voice.claude_pipeline.provider import ClaudePipelineProvider  # noqa: E402

AVATAR = Path(__file__).resolve().parents[2] / "robots" / "avatar"
LOUD = b"\x11\x22" * 240
SILENCE = b"\x00\x00" * 240

SAM_SEEN = PersonSeen(
    track_id="t1",
    confidence=0.92,
    bearing=None,
    distance_class="near",
    bbox_norm=(0.40, 0.30, 0.20, 0.35),
    face_quality=0.81,
)


class LoudVad:
    name = "test"

    def feed(self, pcm16: bytes) -> bool:
        return bool(pcm16) and max(pcm16) > 8

    def reset(self) -> None:
        return None


@pytest.fixture
def client() -> FakeAnthropic:
    """Sam introduces himself; Claude remembers him, then answers."""
    return FakeAnthropic(
        tool_turn("remember_person", {"name": "Sam"}, "toolu_sam", preface="Enchante Sam !"),
        text_turn("Je me souviendrai de toi, Sam."),
    )


@pytest.fixture
async def runtime(client, tmp_path):
    """A whole runtime whose voice provider is the real Claude pipeline.

    ``audio_enabled=False``: no microphone in CI, and the audio loop has its
    own tests. Everything else is the production path.
    """
    config = load_robot_config(AVATAR)
    assert config.persona.voice is not None
    assert config.persona.voice.provider == "claude_pipeline"

    def factory() -> ClaudePipelineProvider:
        return ClaudePipelineProvider(
            api_key="unused",
            client=client,
            stt=FakeSTT("Hi, I am Sam."),
            tts=FakeTTS(ms_per_char=1.0),
            turns=TurnDetector(
                config=TurnConfig(min_speech_ms=0.0, end_silence_ms=1.0), vad=LoudVad()
            ),
        )

    instance = Runtime(
        config=config,
        body=FakeBody(),
        voice=factory(),
        faces=(),
        memory=SqliteMemoryStore(tmp_path / "memory.db"),
        voice_factory=factory,
        audio_enabled=False,
        hub_enabled=False,
        tick_s=0.05,
    )
    await instance.start()
    yield instance
    await instance.stop()


async def stand_in_for_perception(runtime: Runtime) -> list[Envelope]:
    received: list[Envelope] = []

    async def on_command(envelope: Envelope) -> None:
        if envelope.kind != "cmd":
            return
        received.append(envelope)
        await runtime.bus.publish(
            envelope.topic, {"ok": True, "samples": 5}, kind="reply", corr=envelope.id
        )

    runtime.bus.subscribe(ENROLL_TOPIC, on_command)
    return received


async def see_sam(runtime: Runtime) -> None:
    await runtime.bus.publish(TOPICS.PERCEPT_PREFIX + "person_seen", SAM_SEEN.to_dict())
    await wait_until(
        lambda: (runtime.bus.latest(TOPICS.SCENE_STATE) or None) is not None
        and runtime.bus.latest(TOPICS.SCENE_STATE).data["people"],
        timeout=3.0,
        message="the mind never published a scene with Sam in it",
    )


async def speak(runtime: Runtime) -> None:
    for _ in range(3):
        await runtime.voice.send_audio(LOUD)
    await runtime.voice.send_audio(SILENCE)


async def test_the_pipeline_gets_real_tool_specs_as_json_schema(runtime, client):
    """The registry's `ToolSpec`s, converted once, on every request."""
    await stand_in_for_perception(runtime)
    await see_sam(runtime)
    await speak(runtime)
    await wait_until(lambda: client.requests, message="no request ever left the provider")

    tools = {tool["name"]: tool for tool in client.requests[0]["tools"]}
    assert {"who_is_here", "remember_person", "stop"} <= set(tools)
    assert tools["remember_person"]["input_schema"]["properties"]["name"]["type"] == "string"
    assert all(isinstance(spec, ToolSpec) for spec in runtime.registry.specs())


async def test_remember_person_flows_through_claude_tool_use(runtime, client):
    """family_evening, first act: Sam says his name and the robot keeps it."""
    enrolled = await stand_in_for_perception(runtime)
    await see_sam(runtime)
    await speak(runtime)

    await wait_until(
        lambda: len(client.requests) == 2,
        timeout=5.0,
        message="the tool result never produced a continuation",
    )
    assert enrolled and enrolled[0].data["name"] == "Sam"

    stored = await runtime.memory.find_person_by_name("Sam")
    assert stored is not None and stored.id == "person:sam"

    results = client.requests[1]["messages"][-1]["content"]
    assert results[0]["tool_use_id"] == "toolu_sam"
    assert '"bound_to_track": true' in results[0]["content"]
    # One user turn, one assistant turn with the tool_use, one tool_result.
    assert [message["role"] for message in client.requests[1]["messages"]] == [
        "user",
        "assistant",
        "user",
    ]


async def test_the_runtime_hears_the_whole_turn(runtime, client):
    heard: list[str] = []

    async def on_utterance(envelope: Envelope) -> None:
        heard.append(envelope.data["text"])

    runtime.bus.subscribe(TOPICS.PERCEPT_PREFIX + "utterance", on_utterance)
    await stand_in_for_perception(runtime)
    await see_sam(runtime)
    await speak(runtime)
    await wait_until(
        lambda: runtime.voice.active_response_id is None and len(client.requests) == 2,
        timeout=5.0,
        message="the turn never finished",
    )
    assert heard == ["Hi, I am Sam."]
    assert runtime.mind.transcript, "the mind never journalled the turn"
