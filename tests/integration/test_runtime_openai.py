"""`Runtime` on top of the real `OpenAIRealtimeProvider`, against a fake server.

The code review's central finding was that nothing assembled these two: the
runtime handed the provider dicts instead of `ToolSpec`s, both of them sent
``response.create`` after a tool result, and the core never knew about the
responses the model started on its own. Every one of those bugs lived in the
joint, and every one of them is asserted here.

The fake server refuses a second concurrent ``response.create`` exactly as
the API does (``conversation_already_has_active_response``), so a duplicate
is a visible failure rather than a silently swallowed error frame.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

VOICE_TESTS = Path(__file__).resolve().parents[1] / "voice"
if str(VOICE_TESTS) not in sys.path:
    sys.path.insert(0, str(VOICE_TESTS))

from fake_realtime_server import FakeRealtimeServer, wait_until  # noqa: E402

from asimoov.contracts.fakes import FakeBody, InMemoryMemoryStore  # noqa: E402
from asimoov.contracts.tools import ToolSpec  # noqa: E402
from asimoov.core.config import load_robot_config  # noqa: E402
from asimoov.core.runtime import Runtime  # noqa: E402
from asimoov.voice.openai_realtime import OpenAIRealtimeProvider  # noqa: E402

AVATAR = Path(__file__).resolve().parents[2] / "robots" / "avatar"
PCM = b"\x11\x22" * 240


@pytest.fixture
async def server():
    instance = FakeRealtimeServer()
    await instance.start()
    yield instance
    await instance.stop()


@pytest.fixture
async def runtime(server):
    """A whole runtime whose voice provider is the real Realtime client.

    ``audio_enabled=False``: this machine has no microphone in CI, and the
    audio loop has its own tests. Everything else is the production path.
    """
    config = load_robot_config(AVATAR)
    started: list[OpenAIRealtimeProvider] = []

    def factory() -> OpenAIRealtimeProvider:
        provider = OpenAIRealtimeProvider(api_key="", url=server.url)
        started.append(provider)
        return provider

    instance = Runtime(
        config=config,
        body=FakeBody(),
        voice=factory(),
        faces=(),
        memory=InMemoryMemoryStore(),
        voice_factory=factory,
        audio_enabled=False,
        hub_enabled=False,
        tick_s=0.05,
    )
    await instance.start()
    yield instance
    await instance.stop()


async def test_the_session_starts_with_real_tool_specs(runtime, server):
    """Blocker 1: dicts made `build_session` loop in reconnection, robot mute."""
    connection = await server.connection(0)
    assert connection.session is not None
    names = {tool["name"] for tool in connection.session["tools"]}
    assert {"who_is_here", "remember_person", "stop"} <= names
    assert all(tool["type"] == "function" for tool in connection.session["tools"])
    assert all(isinstance(spec, ToolSpec) for spec in runtime.registry.specs())


async def test_an_injection_waits_for_a_response_the_model_started(runtime, server):
    """Blocker 4: `response_active` was only true for responses we asked for."""
    connection = await server.connection(0)
    response_id = await connection.start_model_response()
    await wait_until(
        lambda: runtime.injector.response_active,
        message="the runtime never noticed the model's own response",
    )

    await runtime.mind.inject("[Perception] Sam just arrived.")
    await runtime.mind.tick()
    await runtime.mind.tick()
    assert runtime.injector.pending, "the injection was delivered mid-speech"
    assert connection.count("conversation.item.create") == 0

    await connection.finish_response(response_id)
    await wait_until(
        lambda: not runtime.injector.response_active,
        message="the runtime never saw the response end",
    )
    await runtime.mind.tick()
    await connection.wait_for("conversation.item.create")
    injected = [
        event
        for event in connection.received
        if event.get("type") == "conversation.item.create"
    ]
    assert "Sam just arrived" in injected[0]["item"]["content"][0]["text"]


async def test_a_tool_call_answers_with_one_response_create(runtime, server):
    """Blocker 3: the provider and the runtime both asked for a response."""
    connection = await server.connection(0)
    await connection.emit_tool_call("who_is_here", {}, "call_1")
    await connection.wait_for("conversation.item.create")
    await asyncio.sleep(0.1)

    outputs = [
        event
        for event in connection.received
        if event.get("type") == "conversation.item.create"
        and event["item"]["type"] == "function_call_output"
    ]
    assert len(outputs) == 1
    assert '"status": "ok"' in outputs[0]["item"]["output"]
    assert connection.count("response.create") == 1
    assert connection.rejected_responses == 0


async def test_barge_in_truncates_at_what_was_really_heard(runtime, server):
    """The interrupt sequence, driven through the runtime's own controller."""
    connection = await server.connection(0)
    response_id = await connection.start_model_response(item_id="item_1")
    await connection.emit_audio("item_1", PCM)
    await wait_until(
        lambda: runtime.voice.current_item_id == "item_1",
        message="the provider never saw the audio",
    )

    from asimoov.voice.audio.tracker import StreamPlaybackTracker
    from asimoov.voice.barge_in import BargeInController

    tracker = StreamPlaybackTracker(runtime.voice.sample_rate_hz)
    tracker.feed("item_1", PCM)
    controller = BargeInController(runtime.voice, tracker)
    assert await controller.trigger()

    await connection.wait_for("response.cancel")
    assert connection.canceled[0]["response_id"] == response_id
    assert connection.truncations[0]["item_id"] == "item_1"
    assert connection.truncations[0]["audio_end_ms"] >= 0


async def test_a_renewal_carries_the_summary_into_a_second_socket(runtime, server):
    """Major 14: the session manager was built but never wired to the runtime."""
    assert runtime.sessions is not None
    old = runtime.voice
    connection = await server.connection(0)
    connection.summary_text = "Sam asked to dance."

    await runtime.sessions.renew("test")

    fresh = await server.connection(1)
    assert runtime.voice is not old
    assert "Sam asked to dance." in fresh.session["instructions"]
    assert {tool["name"] for tool in fresh.session["tools"]} == {
        tool["name"] for tool in connection.session["tools"]
    }
    await wait_until(
        lambda: connection.closed_at is not None,
        message="the old socket was never closed",
    )
    assert connection.closed_seq > fresh.session_updated_seq


async def test_a_second_concurrent_response_is_refused_by_the_api(runtime, server):
    """The guard behind the three tests above: the fake server really refuses."""
    connection = await server.connection(0)
    response_id = await connection.start_model_response()
    await wait_until(lambda: runtime.voice.active_response_id == response_id)

    # The provider refuses locally, so the API never sees the second create.
    with pytest.raises(RuntimeError):
        await runtime.voice.request_response(None)
    assert connection.count("response.create") == 0

    # And when a caller bypasses the provider's guard, the server refuses it.
    await connection.handle({"type": "response.create"})
    assert connection.rejected_responses == 1
