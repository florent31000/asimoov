"""A scripted stand-in for `anthropic.AsyncAnthropic`, with no network.

It implements only what `ClaudePipelineProvider` uses -- ``messages.stream``
and ``beta.messages.stream``, an async iterator of events, and
``get_final_message()`` -- and records every request payload so a test can
assert on the system blocks, the tools and the beta headers that were sent.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class Usage:
    input_tokens: int = 100
    output_tokens: int = 20
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class Message:
    content: list[Any]
    stop_reason: str = "end_turn"
    stop_details: Any = None
    usage: Usage = field(default_factory=Usage)


@dataclass
class Turn:
    """One scripted response: the events, then the final message."""

    events: list[Any]
    message: Message
    pause_after: int = -1
    resume: asyncio.Event | None = None


def _text_events(text: str, chunk: int) -> list[Any]:
    return [
        SimpleNamespace(type="text", text=text[index : index + chunk])
        for index in range(0, len(text), chunk)
    ]


def text_turn(text: str, *, chunk: int = 8, usage: Usage | None = None, **kwargs: Any) -> Turn:
    return Turn(
        events=_text_events(text, chunk),
        message=Message(content=[TextBlock(text)], usage=usage or Usage()),
        **kwargs,
    )


def tool_turn(
    name: str,
    tool_input: dict[str, Any],
    call_id: str,
    *,
    preface: str = "",
    stop_reason: str = "tool_use",
) -> Turn:
    fragments = json.dumps(tool_input)
    events = _text_events(preface, 8) if preface else []
    events += [
        SimpleNamespace(type="input_json", partial_json=fragments[index : index + 6])
        for index in range(0, len(fragments), 6)
    ]
    content: list[Any] = [TextBlock(preface)] if preface else []
    content.append(ToolUseBlock(id=call_id, name=name, input=tool_input))
    return Turn(events=events, message=Message(content=content, stop_reason=stop_reason))


def refusal_turn(category: str = "cyber") -> Turn:
    return Turn(
        events=[],
        message=Message(
            content=[TextBlock("")],
            stop_reason="refusal",
            stop_details=SimpleNamespace(category=category, explanation="declined"),
        ),
    )


class FakeStream:
    def __init__(self, turn: Turn) -> None:
        self._turn = turn

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    def __aiter__(self) -> Any:
        return self._iterate()

    async def _iterate(self) -> Any:
        for index, event in enumerate(self._turn.events):
            await asyncio.sleep(0)
            yield event
            if index == self._turn.pause_after and self._turn.resume is not None:
                await self._turn.resume.wait()

    async def get_final_message(self) -> Message:
        return self._turn.message


class _Messages:
    def __init__(self, owner: FakeAnthropic) -> None:
        self._owner = owner

    def stream(self, **kwargs: Any) -> FakeStream:
        self._owner.requests.append(kwargs)
        if not self._owner.script:
            raise AssertionError("the fake Anthropic client ran out of scripted turns")
        return FakeStream(self._owner.script.pop(0))


class FakeAnthropic:
    """``.messages`` and ``.beta.messages`` both play the same script."""

    def __init__(self, *turns: Turn) -> None:
        self.script: list[Turn] = list(turns)
        self.requests: list[dict[str, Any]] = []
        self.closed = False
        self.messages = _Messages(self)
        self.beta = SimpleNamespace(messages=self.messages)

    def push(self, *turns: Turn) -> None:
        self.script.extend(turns)

    async def close(self) -> None:
        self.closed = True


def assert_valid_history(messages: list[dict[str, Any]]) -> None:
    """The Messages API placement rules, which the fake does not enforce.

    Breaking one of them is silent in a test and a 400 on every later
    request in production -- and two of the three are permanent, because the
    offending message stays in the history.
    """
    assert messages, "the API rejects an empty `messages`"
    assert messages[0]["role"] == "user", f"first message is {messages[0]['role']!r}"
    for index, message in enumerate(messages):
        if message["role"] != "system":
            continue
        assert index and messages[index - 1]["role"] == "user", (
            f"system at {index} follows {messages[index - 1]['role']!r}, not 'user'"
        )
        assert index == len(messages) - 1 or messages[index + 1]["role"] == "assistant", (
            f"system at {index} is followed by {messages[index + 1]['role']!r}"
        )

    def ids(kind: str, field: str) -> set[str]:
        return {
            block[field]
            for message in messages
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == kind
        }

    orphans = ids("tool_use", "id") - ids("tool_result", "tool_use_id")
    assert not orphans, f"tool_use without a tool_result: {sorted(orphans)}"
