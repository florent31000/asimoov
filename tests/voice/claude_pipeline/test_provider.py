"""`ClaudePipelineProvider`: one whole turn, tools, cancel, injection, renewal.

No network and no API key: the Anthropic client is scripted
(`fake_anthropic.py`), the STT returns a fixed transcript and the TTS a tone.
What is asserted is the joint -- what reaches `VoiceEvents`, and what shape
the request that left the provider had.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fake_anthropic import FakeAnthropic, Usage, refusal_turn, text_turn, tool_turn
from fake_realtime_server import wait_until

from asimoov.contracts.behaviors import BehaviorStatus
from asimoov.contracts.tools import ToolResult, ToolSpec
from asimoov.voice.claude_pipeline.provider import (
    FALLBACK_BETA,
    LATENCY_INSTRUCTION,
    OPENING_SEED,
    ClaudePipelineProvider,
    tool_to_claude,
)

LOUD = b"\x11\x22" * 240
SILENCE = b"\x00\x00" * 240


async def speak(provider, *, chunks: int = 3) -> None:
    """Drive one complete user utterance through the local turn detector."""
    for _ in range(chunks):
        await provider.send_audio(LOUD)
    await provider.send_audio(SILENCE)




async def test_a_whole_turn_reaches_the_core(start_provider, events):
    client = FakeAnthropic(text_turn("Bonjour Sam. Comment vas-tu aujourd'hui ?"))
    provider = await start_provider(client, transcripts="Salut.")

    await speak(provider)
    await wait_until(lambda: events.responses_done, message="the turn never finished")

    assert events.speech_started == 1
    assert events.speech_ended == 1
    assert [utterance.text for utterance in events.utterances] == ["Salut."]
    assert [utterance.final for utterance in events.utterances] == [True]
    assert events.responses_started == events.responses_done
    assert events.audio, "nothing was ever synthesized"
    assert len({item_id for item_id, _ in events.audio}) == 1
    assert "Bonjour Sam." in events.assistant_text[0]
    assert provider.active_response_id is None


async def test_the_request_carries_the_cached_prompt_and_the_latency_rule(
    start_provider, events
):
    client = FakeAnthropic(text_turn("Oui, tout va bien merci."))
    provider = await start_provider(client)

    await speak(provider)
    await wait_until(lambda: events.responses_done)

    request = client.requests[0]
    assert request["betas"] == [FALLBACK_BETA]
    assert request["fallbacks"] == "default"
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"] == {"effort": "low"}
    system = request["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert LATENCY_INSTRUCTION in system[0]["text"]
    assert all(tool["eager_input_streaming"] for tool in request["tools"])
    assert request["messages"][0] == {"role": "user", "content": "Bonjour."}


async def test_a_tool_call_runs_once_and_continues_the_same_turn(start_provider, events):
    client = FakeAnthropic(
        tool_turn("gesture", {"name": "wave"}, "toolu_1"),
        text_turn("Voila, je te fais coucou !"),
    )
    provider = await start_provider(client)

    await speak(provider)
    await wait_until(lambda: events.tool_calls, message="the tool call never arrived")
    assert events.tool_calls == [("gesture", {"name": "wave"}, "toolu_1")]
    assert provider.has_pending_tool_call

    await provider.send_tool_result("toolu_1", ToolResult(status=BehaviorStatus.OK))
    await wait_until(lambda: events.responses_done, message="the turn never finished")

    # One response for the whole exchange, and a single continuation request.
    assert len(events.responses_started) == 1
    assert len(client.requests) == 2
    results = client.requests[1]["messages"][-1]["content"]
    assert results[0]["type"] == "tool_result"
    assert results[0]["tool_use_id"] == "toolu_1"
    assert '"status": "ok"' in results[0]["content"]
    assert not provider.has_pending_tool_call


async def test_invalid_tool_arguments_never_reach_the_core(start_provider, events):
    """Eager streaming turns off server validation, so the schema is checked here."""
    client = FakeAnthropic(
        tool_turn("gesture", {"name": 42}, "toolu_1"),
        text_turn("Je n'ai pas compris ce geste."),
    )
    provider = await start_provider(client)

    await speak(provider)
    await wait_until(lambda: events.responses_done)

    assert events.tool_calls == []
    results = client.requests[1]["messages"][-1]["content"]
    assert results[0]["is_error"] is True
    assert "invalid_arguments" in results[0]["content"]


async def test_cancel_mid_stream_stops_the_speech(start_provider, events):
    resume = asyncio.Event()
    turn = text_turn(
        "Je te raconte. Une longue histoire qui commence un matin.",
        chunk=4,
        pause_after=4,
    )
    turn.resume = resume
    client = FakeAnthropic(turn)
    provider = await start_provider(client)

    await speak(provider)
    await wait_until(lambda: events.audio, message="nothing was spoken before the cancel")
    spoken_before = len(events.audio)

    await provider.cancel_response(provider.active_response_id)
    resume.set()
    await wait_until(lambda: events.responses_done, message="the cancelled turn never ended")

    assert len(events.audio) == spoken_before, "audio kept coming after the cancel"
    # What the model believes it said is exactly what was spoken, no more.
    assistant = [m for m in provider.messages if m["role"] == "assistant"]
    assert assistant, "the partial answer was lost"
    partial = assistant[-1]["content"][0]["text"]
    assert partial == "Je te raconte."


async def test_truncate_trims_the_history_to_what_was_heard(start_provider, events):
    client = FakeAnthropic(text_turn("Une phrase entiere, puis une autre phrase."))
    provider = await start_provider(client)

    await speak(provider)
    await wait_until(lambda: events.responses_done)

    item_id = events.audio[0][0]
    full = [m for m in provider.messages if m["role"] == "assistant"][-1]["content"][0]["text"]
    total_ms = sum(len(pcm) / 2 * 1000 / provider.sample_rate_hz for _, pcm in events.audio)

    await provider.truncate_item(item_id, total_ms / 2)
    kept = [m for m in provider.messages if m["role"] == "assistant"][-1]["content"][0]["text"]
    assert 0 < len(kept) < len(full)
    assert full.startswith(kept)


async def test_an_injection_is_buffered_then_sent_as_a_system_message(start_provider, events):
    resume = asyncio.Event()
    first = text_turn("Bonjour a toi aussi.", chunk=4, pause_after=1)
    first.resume = resume
    client = FakeAnthropic(first, text_turn("Oui, Sam est bien la."))
    provider = await start_provider(client)

    await speak(provider)
    await wait_until(lambda: provider.active_response_id is not None)
    # Injected mid-turn: it must not reach the request being streamed.
    await provider.inject_system_text("[Perception] Sam vient d'arriver.")
    resume.set()
    await wait_until(lambda: events.responses_done)
    assert all(message["role"] != "system" for message in client.requests[0]["messages"])

    await speak(provider)
    await wait_until(lambda: len(events.responses_done) == 2)
    second = client.requests[1]
    # The cached prefix is untouched: the injection is a message, not a block.
    assert len(second["system"]) == 1
    injected = [m for m in second["messages"] if m["role"] == "system"]
    assert injected and "Sam vient d'arriver" in injected[-1]["content"]
    assert second["messages"][-1] is injected[-1]


async def test_initiative_opens_a_conversation_nobody_started(start_provider, events):
    client = FakeAnthropic(text_turn("Salut Sam, content de te voir !"))
    provider = await start_provider(client)

    await provider.request_response("Salue Sam, il vient d'arriver.")
    await wait_until(lambda: events.responses_done, message="the initiative never ran")

    request = client.requests[0]
    assert request["messages"] == [{"role": "user", "content": OPENING_SEED}]
    assert request["system"][1]["text"] == "Salue Sam, il vient d'arriver."
    assert events.audio


async def test_a_refusal_is_handled_before_the_content(start_provider, events, caplog):
    """A decline can cut a `tool_use` off mid-input, so nothing runs."""
    refusal = refusal_turn("cyber")
    refusal.message.content.append(
        tool_turn("gesture", {"name": "wave"}, "toolu_1").message.content[0]
    )
    client = FakeAnthropic(refusal)
    provider = await start_provider(client)

    with caplog.at_level(logging.WARNING):
        await speak(provider)
        await wait_until(lambda: events.responses_done)

    assert events.tool_calls == [], "a refused tool_use was executed"
    assert events.audio == []
    assert "refused" in caplog.text
    assert provider.active_response_id is None


async def test_a_refusal_hands_the_injection_back_instead_of_stranding_it(
    start_provider, events
):
    """A `system` message with no assistant after it poisons every later request."""
    client = FakeAnthropic(refusal_turn(), text_turn("Oui, Sam est bien la."))
    provider = await start_provider(client)

    await provider.inject_system_text("[Perception] Sam vient d'arriver.")
    await speak(provider)
    await wait_until(lambda: events.responses_done)
    assert all(message["role"] != "system" for message in provider.messages)

    await speak(provider)
    await wait_until(lambda: len(events.responses_done) == 2)
    injected = [m for m in client.requests[1]["messages"] if m["role"] == "system"]
    assert injected and "Sam vient d'arriver" in injected[-1]["content"]


async def test_an_initiative_after_an_answer_keeps_the_injection_out_of_the_history(
    start_provider, events
):
    """`request_response` does not follow a user turn, so no system message fits."""
    client = FakeAnthropic(text_turn("Bonjour a toi."), text_turn("Salut Sam !"))
    provider = await start_provider(client)

    await speak(provider)
    await wait_until(lambda: events.responses_done)
    await provider.inject_system_text("[Perception] Sam vient d'arriver.")
    await provider.request_response("Salue Sam.")
    await wait_until(lambda: len(events.responses_done) == 2)

    second = client.requests[1]
    assert [block["text"] for block in second["system"][1:]] == [
        "[Perception] Sam vient d'arriver.",
        "Salue Sam.",
    ]
    assert all(message["role"] != "system" for message in second["messages"])


async def test_the_renewal_summary_stays_out_of_the_history(start_provider, events):
    client = FakeAnthropic(text_turn("Enchante Sam."), text_turn("Sam s'est presente."))
    provider = await start_provider(client)

    await speak(provider)
    await wait_until(lambda: events.responses_done)
    before = provider.messages

    summary = await provider.request_summary("Resume la conversation.")
    assert summary == "Sam s'est presente."
    assert provider.messages == before
    assert "tools" not in client.requests[-1]
    assert client.requests[-1]["messages"][-1]["content"].startswith("Resume la conversation.")


async def test_a_second_sentence_during_a_turn_is_answered_not_dropped(
    start_provider, events
):
    """The mic stays open while the model thinks; losing that is a silent failure."""
    resume = asyncio.Event()
    first = text_turn("Je reflechis un instant.", chunk=4, pause_after=1)
    first.resume = resume
    client = FakeAnthropic(first, text_turn("Et voila ma reponse."))
    provider = await start_provider(client, transcripts=["un", "deux"])

    await speak(provider)
    await wait_until(lambda: provider.active_response_id is not None)
    await speak(provider)
    await wait_until(lambda: len(events.utterances) == 2)
    resume.set()
    await wait_until(
        lambda: len(events.responses_done) == 2,
        message="the second sentence never got its own turn",
    )
    assert [message["content"] for message in provider.messages if message["role"] == "user"] == [
        "un",
        "deux",
    ]


async def test_stopping_during_a_tool_call_leaves_nothing_running(start_provider, events):
    client = FakeAnthropic(tool_turn("gesture", {"name": "wave"}, "toolu_1"))
    provider = await start_provider(client)

    await speak(provider)
    await wait_until(lambda: events.tool_calls, message="the tool call never arrived")

    await provider.stop()
    done_at_stop = list(events.responses_done)
    await asyncio.sleep(0.05)

    assert events.responses_done == done_at_stop, "a callback fired after stop()"
    assert provider.messages == [], "a tool_use with no tool_result survived stop()"


async def test_truncating_a_turn_that_spoke_around_a_tool_call(start_provider, events):
    """Two assistant messages, two budgets: the preface must not eat the answer."""
    client = FakeAnthropic(
        tool_turn("gesture", {"name": "wave"}, "toolu_1", preface="Bien sur, j'y vais."),
        text_turn("Voila, c'est fait pour toi."),
    )
    provider = await start_provider(client)
    await speak(provider)
    await wait_until(lambda: events.tool_calls)
    await provider.send_tool_result("toolu_1", ToolResult(status=BehaviorStatus.OK))
    await wait_until(lambda: events.responses_done)

    item_id = events.audio[0][0]
    total_ms = sum(len(pcm) / 2 * 1000 / provider.sample_rate_hz for _, pcm in events.audio)
    await provider.truncate_item(item_id, total_ms)

    said = [
        block["text"]
        for message in provider.messages
        if message["role"] == "assistant"
        for block in message["content"]
        if block["type"] == "text"
    ]
    assert said == ["Bien sur, j'y vais.", "Voila, c'est fait pour toi."]

    await provider.truncate_item(item_id, 0.0)
    trimmed = [
        block["text"]
        for message in provider.messages
        if message["role"] == "assistant"
        for block in message["content"]
        if block["type"] == "text"
    ]
    assert trimmed == ["…", "…"]


async def test_the_provider_reports_usage_for_the_session_manager(start_provider):
    seen: list[dict] = []
    client = FakeAnthropic(
        text_turn("Bonjour a tous.", usage=Usage(input_tokens=900, output_tokens=100))
    )
    provider = await start_provider(client, on_usage=seen.append)

    await speak(provider)
    await wait_until(lambda: seen, message="usage was never reported")
    assert seen[0]["total_tokens"] == 1000


async def test_request_response_refuses_to_talk_over_itself(start_provider, events):
    resume = asyncio.Event()
    turn = text_turn("Je parle deja, laisse-moi finir.", chunk=4, pause_after=2)
    turn.resume = resume
    client = FakeAnthropic(turn)
    provider = await start_provider(client)

    await speak(provider)
    await wait_until(lambda: provider.active_response_id is not None)
    with pytest.raises(RuntimeError):
        await provider.request_response("Dis bonjour.")
    resume.set()
    await wait_until(lambda: events.responses_done)


async def test_pruning_never_orphans_a_tool_result(start_provider, events):
    client = FakeAnthropic(
        tool_turn("gesture", {"name": "wave"}, "toolu_1"),
        text_turn("C'est fait, coucou !"),
        text_turn("Avec plaisir, a bientot."),
    )
    provider = await start_provider(client)
    await speak(provider)
    await wait_until(lambda: events.tool_calls)
    await provider.send_tool_result("toolu_1", ToolResult(status=BehaviorStatus.OK))
    await wait_until(lambda: events.responses_done)
    await speak(provider)
    await wait_until(lambda: len(events.responses_done) == 2)

    # user, assistant(tool_use), user(tool_result), assistant, user, assistant
    assert len(provider.messages) == 6
    assert await provider.prune_items(max_items=3) == ["msg_0", "msg_1", "msg_2", "msg_3"]
    kept = provider.messages
    assert kept[0]["role"] == "user" and isinstance(kept[0]["content"], str)


def test_the_api_key_is_read_from_secrets_and_never_logged(monkeypatch, caplog, tmp_path):
    monkeypatch.setenv("ASIMOOV_HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ASIMOOV_ANTHROPIC_API_KEY", "sk-ant-secret-value")

    from asimoov.core.config import Secrets
    from asimoov.core.runtime import _voice_factory

    assert Secrets().get(ClaudePipelineProvider.secret_key) == "sk-ant-secret-value"
    with caplog.at_level(logging.DEBUG):
        provider = _voice_factory(ClaudePipelineProvider)()
    assert isinstance(provider, ClaudePipelineProvider)
    assert "sk-ant-secret-value" not in caplog.text
    assert "sk-ant-secret-value" not in repr(provider)


def test_the_openai_key_never_reaches_the_claude_provider(monkeypatch, tmp_path):
    """`_voice_factory` used to hand every provider the OpenAI key."""
    monkeypatch.setenv("ASIMOOV_HOME", str(tmp_path))
    monkeypatch.setenv("ASIMOOV_OPENAI_API_KEY", "sk-openai")
    monkeypatch.delenv("ASIMOOV_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from asimoov.core.runtime import _voice_factory

    provider = _voice_factory(ClaudePipelineProvider)()
    assert provider._api_key is None


def test_strict_is_only_set_on_a_schema_that_can_carry_it():
    open_schema = ToolSpec(name="a", description="", params={"type": "object", "properties": {}})
    closed = ToolSpec(
        name="b",
        description="",
        params={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    assert "strict" not in tool_to_claude(open_schema)
    assert tool_to_claude(closed)["strict"] is True
    assert "eager_input_streaming" not in tool_to_claude(closed, streaming=False)
