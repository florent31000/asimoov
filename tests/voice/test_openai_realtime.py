"""Provider behaviour against the fake Realtime server."""

from __future__ import annotations

import json

import pytest
from conftest import BASE_CONFIG, TOOLS
from fake_realtime_server import wait_until

from asimoov.contracts.behaviors import BehaviorStatus
from asimoov.contracts.tools import ToolResult, ToolSpec
from asimoov.voice.openai_realtime import OpenAIRealtimeProvider, build_session


async def test_session_update_matches_the_ga_shape(server, start_provider):
    await start_provider()
    connection = await server.connection()

    session = connection.session
    assert session["type"] == "realtime"
    assert session["output_modalities"] == ["audio"]
    assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert session["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert session["audio"]["input"]["noise_reduction"] == {"type": "near_field"}
    assert session["audio"]["input"]["turn_detection"]["type"] == "server_vad"
    assert session["audio"]["input"]["transcription"]["language"] == "fr"
    assert session["tools"][0]["name"] == "gesture"
    assert session["tools"][0]["type"] == "function"
    assert session["tool_choice"] == "auto"


def test_semantic_vad_is_selectable():
    session = build_session(
        {"turn_detection": "semantic_vad", "eagerness": "high"}, sample_rate_hz=24000
    )
    assert session["audio"]["input"]["turn_detection"] == {
        "type": "semantic_vad",
        "eagerness": "high",
    }


def test_unknown_turn_detection_is_rejected():
    with pytest.raises(ValueError):
        build_session({"turn_detection": "magic"}, sample_rate_hz=24000)


async def test_tool_call_sends_the_real_result_and_waits_for_an_idle_response(
    server, events, start_provider
):
    provider = await start_provider()
    connection = await server.connection()

    await connection.emit_response_created("resp_a")
    await connection.emit_tool_call("gesture", {"name": "wave"}, "call_1")
    await wait_until(lambda: bool(events.tool_calls), message="no tool call surfaced")

    assert events.tool_calls == [("gesture", {"name": "wave"}, "call_1")]
    assert provider.has_pending_tool_call is True
    assert provider.active_response_id == "resp_a"

    result = ToolResult(status=BehaviorStatus.OK, content={"gesture": "wave"})
    await provider.send_tool_result("call_1", result)
    await connection.wait_for("conversation.item.create")

    outputs = [e for e in connection.received if e["type"] == "conversation.item.create"]
    assert outputs[-1]["item"]["type"] == "function_call_output"
    assert json.loads(outputs[-1]["item"]["output"]) == {
        "status": "ok",
        "content": {"gesture": "wave"},
    }
    # The response that issued the call is still streaming: no response.create.
    assert connection.count("response.create") == 0
    assert provider.has_pending_tool_call is False

    await connection.emit_response_done("resp_a")
    await wait_until(lambda: provider.active_response_id is None)
    await provider.send_tool_result("call_2", result)
    await connection.wait_for("response.create")
    assert connection.count("response.create") == 1


async def test_cancel_carries_the_response_id(server, start_provider):
    provider = await start_provider()
    connection = await server.connection()

    with pytest.raises(ValueError):
        await provider.cancel_response("")

    await provider.cancel_response("resp_7")
    await connection.wait_for("response.cancel")
    assert connection.canceled[0]["response_id"] == "resp_7"


async def test_truncate_uses_played_ms(server, start_provider):
    provider = await start_provider()
    connection = await server.connection()

    await provider.truncate_item("item_9", 812.6)
    await connection.wait_for("conversation.item.truncate")
    assert connection.truncations[0] == {
        "type": "conversation.item.truncate",
        "item_id": "item_9",
        "content_index": 0,
        "audio_end_ms": 812,
    }


async def test_inject_system_text_creates_a_system_item(server, start_provider):
    provider = await start_provider()
    connection = await server.connection()

    await provider.inject_system_text("[Perception 14:02] Sam vient d'arriver.")
    await connection.wait_for("conversation.item.create")
    item = connection.received[-1]["item"]
    assert item["role"] == "system"
    assert item["content"][0]["text"].startswith("[Perception 14:02]")
    assert connection.count("response.create") == 0


async def test_request_response_during_an_active_response_is_a_caller_bug(server, start_provider):
    provider = await start_provider()
    connection = await server.connection()

    await connection.emit_response_created("resp_b")
    await wait_until(lambda: provider.active_response_id == "resp_b")
    with pytest.raises(RuntimeError):
        await provider.request_response("say hello")


async def test_transcripts_are_never_filtered(server, events, start_provider):
    await start_provider()
    connection = await server.connection()

    await connection.emit_transcript("hmm", final=True)
    await connection.emit_transcript("euh bon", final=False)
    await wait_until(lambda: len(events.utterances) == 2)

    assert [(u.text, u.final) for u in events.utterances] == [("hmm", True), ("euh bon", False)]
    assert events.utterances[0].lang == "fr"


async def test_audio_out_and_telemetry_timestamps(server, events, start_provider):
    stamps: list[str] = []
    await start_provider(on_timestamp=lambda name, ts: stamps.append(name))
    connection = await server.connection()

    await connection.emit_speech_started()
    await connection.emit_speech_stopped()
    await connection.emit_response_created("resp_c")
    await connection.emit_audio("item_1", b"\x01\x00" * 240)
    await wait_until(lambda: bool(events.audio))

    assert events.speech_started == 1
    assert events.speech_ended == 1
    assert events.audio[0][0] == "item_1"
    assert len(events.audio[0][1]) == 480
    assert stamps == ["speech_started_ts", "speech_stopped_ts", "first_audio_delta_ts"]


async def test_response_done_reports_usage_and_clears_the_active_response(
    server, events, start_provider
):
    usages: list[dict] = []
    provider = await start_provider(on_usage=usages.append)
    connection = await server.connection()

    await connection.emit_response_created("resp_d")
    await connection.emit_response_done("resp_d", total_tokens=1234)
    await wait_until(lambda: bool(events.responses_done))

    assert events.responses_done == ["resp_d"]
    assert usages == [{"total_tokens": 1234}]
    assert provider.active_response_id is None


async def test_prune_items_keeps_the_first_system_item(server, start_provider):
    provider = await start_provider()
    connection = await server.connection()

    await connection.emit_item_created("item_sys", role="system")
    for index in range(6):
        await connection.emit_item_created(f"item_{index}")
    await wait_until(lambda: len(provider.item_ids) == 7)

    deleted = await provider.prune_items(max_items=3)
    assert deleted == ["item_0", "item_1", "item_2", "item_3"]
    await connection.wait_for("conversation.item.delete", count=4)
    await wait_until(lambda: provider.item_ids == ["item_sys", "item_4", "item_5"])


async def test_wss_without_a_key_is_refused(events):
    provider = OpenAIRealtimeProvider(api_key="", url="wss://api.openai.com/v1/realtime")
    with pytest.raises(ValueError):
        await provider.start(events, {})


def test_build_session_refuses_serialized_tool_dicts():
    """Blocker 1: dicts made the socket reconnect forever instead of failing."""
    spec = TOOLS[0]
    with pytest.raises(TypeError, match="ToolSpec"):
        build_session({**BASE_CONFIG, "tools": [spec.to_dict()]}, sample_rate_hz=24000)
    with pytest.raises(TypeError, match="ToolSpec"):
        build_session({**BASE_CONFIG, "tools": {"gesture": spec}}, sample_rate_hz=24000)


async def test_start_fails_loudly_on_a_malformed_session(server, events):
    """The error must reach the caller, not an unattended reconnect task."""
    provider = OpenAIRealtimeProvider(api_key="", url=server.url)
    with pytest.raises(TypeError):
        await provider.start(events, {**BASE_CONFIG, "tools": [{"name": "gesture"}]})
    assert provider.is_ready is False
    await provider.stop()


async def test_a_response_the_model_starts_is_announced(start_provider, server, events):
    """v1.3 `on_response_started`: the core must know it is being spoken over."""
    provider = await start_provider()
    connection = await server.connection(0)
    response_id = await connection.start_model_response()
    await wait_until(lambda: provider.active_response_id == response_id)
    assert events.responses_started == [response_id]


async def test_the_summary_is_attributed_by_its_metadata_not_its_order(
    start_provider, server, events
):
    """Medium: a model-initiated response used to steal the summary's slot."""
    provider = await start_provider()
    connection = await server.connection(0)
    connection.interleave_model_response = True

    summary = await provider.request_summary("Summarize.", timeout_s=2.0)

    assert summary == connection.summary_text
    # The model's own response is still the active one: it was not mistaken
    # for the summary just because it arrived first.
    assert provider.active_response_id == "resp_0_1"
    assert events.responses_started == ["resp_0_1"]


async def test_update_tools_republishes_the_session(start_provider, server):
    """Major 11: a rebound manifest changes which tools exist."""
    provider = await start_provider()
    connection = await server.connection(0)
    extra = ToolSpec(name="move", description="Walk.", params={"type": "object"})

    await provider.update_tools((*TOOLS, extra))

    await connection.wait_for("session.update", count=2)
    assert {tool["name"] for tool in connection.session["tools"]} == {"gesture", "move"}
