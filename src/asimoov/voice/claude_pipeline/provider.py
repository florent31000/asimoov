"""`VoiceProvider` built on the Claude Messages API: VAD -> STT -> Claude -> TTS.

The Messages API takes text and images; it has no audio input and no speech
output, so there is no speech-to-speech session to open. This provider
assembles one out of local parts and presents the same frozen ABC as
`openai_realtime.OpenAIRealtimeProvider`, so `VoiceLoop`, `MicGate`,
`BargeInController` and `SessionManager` work unchanged.

```
mic PCM16 --> TurnDetector (Silero) --> utterance buffer
                                             |
                                        SpeechToText
                                             |
             Claude (streaming text + tool_use, adaptive thinking)
                                             |
                         sentence chunks --> TextToSpeech --> on_audio_out
```

Everything the Realtime API decided for us is decided here: when a turn ends
(`turns.py`), what the robot says out loud (`_feed_text` and the TTS queue),
and what the conversation contains (`_messages`, trimmed by `truncate_item`
and `prune_items`).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import jsonschema

from asimoov.contracts.behaviors import BehaviorStatus
from asimoov.contracts.percepts import Utterance
from asimoov.contracts.tools import ToolResult, ToolSpec
from asimoov.contracts.voice import VoiceEvents, VoiceProvider
from asimoov.core.timeouts import run_with_timeout
from asimoov.voice.claude_pipeline.stt import SpeechToText, build_stt
from asimoov.voice.claude_pipeline.tts import TextToSpeech, build_tts
from asimoov.voice.claude_pipeline.turns import (
    DEFAULT_END_SILENCE_MS,
    SPEECH_ENDED,
    SPEECH_STARTED,
    TurnConfig,
    TurnDetector,
)

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "low"
DEFAULT_MAX_TOKENS = 1024
#: The model spends far less time thinking before the first visible token
#: with this in the system prompt; it is what makes the route usable by voice.
LATENCY_INSTRUCTION = "Latency-sensitive; begin your visible answer immediately."
FALLBACK_BETA = "server-side-fallback-2026-07-01"
SUMMARY_SUFFIX = "Answer with plain text only, at most 5 lines."
#: The API needs at least one message and a mid-conversation system message
#: cannot be the first one, so a turn the robot starts by itself opens on
#: this instead of on a made-up human line.
OPENING_SEED = "(The conversation has not started; nobody has spoken yet.)"
#: Audio kept before the detector latches, so the first syllables of an
#: utterance reach the transcriber instead of being cut off.
PREROLL_MS = 500.0
SENTENCE_END = ".!?…:;\n"
MIN_CHUNK_CHARS = 12
MAX_TOOL_ROUNDS = 4
TOOL_RESULT_MARGIN_S = 5.0
#: How many past assistant items stay truncatable. Barge-in only ever
#: truncates the item that is playing, so a handful is generous.
KEPT_ITEMS = 8


def tool_to_claude(spec: ToolSpec, *, streaming: bool = True) -> dict[str, Any]:
    """Convert a frozen `ToolSpec` into a Messages API client tool.

    ``eager_input_streaming`` is the documented default for a streaming
    request with client tools. ``strict`` is only set on a schema that can
    carry it -- a closed object whose properties are all required -- because
    the API rejects it on any other shape.
    """
    tool: dict[str, Any] = {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.params,
    }
    if streaming:
        tool["eager_input_streaming"] = True
    if _strict_capable(spec.params):
        tool["strict"] = True
    return tool


def _strict_capable(schema: dict[str, Any]) -> bool:
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return False
    if schema.get("additionalProperties") is not False:
        return False
    # A nested object would need the same closure applied all the way down;
    # claiming strictness without checking it is a 400 waiting to happen.
    if any(prop.get("type") in ("object", "array") for prop in properties.values()):
        return False
    return set(schema.get("required", ())) == set(properties)


def _valid_input(schema: dict[str, Any], value: Any) -> bool:
    """True when ``value`` satisfies ``schema``.

    Eager input streaming turns off the server's own validation, and the
    SDK's tolerant parser returns a silently truncated object rather than
    raising, so the input is checked here before a tool ever runs. A
    malformed ``ToolSpec.params`` is a programming error and raises
    `jsonschema.SchemaError` rather than turning every call into a refusal.
    """
    if not isinstance(value, dict):
        return False
    try:
        jsonschema.validate(value, schema)
    except jsonschema.ValidationError:
        return False
    return True


@dataclass
class _Segment:
    """What one round of a turn actually said, and where it lives in history.

    Kept per round rather than per turn: a turn that speaks, calls a tool and
    speaks again writes two assistant messages, and `truncate_item` has to
    know which text belongs to which one.
    """

    text: str = ""
    audio_ms: float = 0.0
    index: int | None = None


def _block_to_dict(block: Any) -> dict[str, Any]:
    """The history form of one assistant content block.

    Unknown block types (thinking, and whatever the API adds next) are kept
    verbatim: dropping them breaks the tool continuation that follows.
    """
    kind = getattr(block, "type", None)
    if kind == "text":
        return {"type": "text", "text": block.text}
    if kind == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    to_dict = getattr(block, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return dict(block) if isinstance(block, dict) else {"type": str(kind)}


class ClaudePipelineProvider(VoiceProvider):
    """Speech-to-speech over the Claude Messages API, assembled locally.

    Args:
        api_key: Anthropic key. Defaults to `core.config.Secrets` under
            ``anthropic_api_key``. Never logged, never printed.
        client: An `anthropic.AsyncAnthropic`, or anything exposing the same
            ``messages.stream`` / ``beta.messages.stream``. Tests inject a
            fake here; production leaves it None and the SDK is built lazily.
        stt, tts, turns: Override the engines and the turn detector the
            persona names.
        fallbacks: Opt into server-side refusal fallbacks (recommended).
        on_timestamp: ``(name, unix_seconds)`` telemetry, the same three
            names as the Realtime provider.
        on_assistant_text: The assistant transcript of a finished response.
        on_usage: The usage of a finished response, wired to
            `session.SessionManager.note_usage` by the core.
    """

    sample_rate_hz = 24000
    #: Which `core.config.Secrets` key holds this provider's API key.
    secret_key = "anthropic_api_key"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        client: Any = None,
        stt: SpeechToText | None = None,
        tts: TextToSpeech | None = None,
        turns: TurnDetector | None = None,
        fallbacks: bool = True,
        on_timestamp: Callable[[str, float], None] | None = None,
        on_assistant_text: Callable[[str], None] | None = None,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._api_key = api_key
        self._client = client
        self._owns_client = client is None
        self._stt = stt
        self._tts = tts
        self._fallbacks = fallbacks
        self._on_timestamp = on_timestamp
        self._on_assistant_text = on_assistant_text
        self._on_usage = on_usage

        self._events: VoiceEvents | None = None
        self._config: dict[str, Any] = {}
        self._system_prompt = ""
        self._model = DEFAULT_MODEL
        self._effort = DEFAULT_EFFORT
        self._max_tokens = DEFAULT_MAX_TOKENS
        self._language: str | None = None
        self._tools: tuple[ToolSpec, ...] = ()
        self._tools_by_name: dict[str, ToolSpec] = {}

        self._running = False
        self._ready = asyncio.Event()
        self._turns = turns
        self._end_silence_ms = DEFAULT_END_SILENCE_MS
        self._preroll: deque[bytes] = deque()
        self._preroll_bytes = 0
        self._utterance: bytearray | None = None

        self._messages: list[dict[str, Any]] = []
        self._pending_system: list[str] = []
        self._system_index: int | None = None
        self._spoken: dict[str, list[_Segment]] = {}

        self._turn_lock = asyncio.Lock()
        self._sequence = itertools.count(1)
        self._active_response_id: str | None = None
        self._current_item_id: str | None = None
        self._round = 0
        self._cancelled = False
        self._say_buffer = ""
        self._tool_futures: dict[str, asyncio.Future[ToolResult]] = {}
        self._queue: asyncio.Queue[tuple[str, int, str] | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._warmup: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    # ---------------------------------------------------------------- ABC

    async def start(self, events: VoiceEvents, config: dict[str, Any]) -> None:
        if self._running:
            raise RuntimeError("provider already started")
        tools = config.get("tools", ())
        if isinstance(tools, (str, bytes, dict)):
            raise TypeError(
                f"config['tools'] must be a sequence of ToolSpec, got {type(tools).__name__}"
            )
        wrong = sorted({type(spec).__name__ for spec in tools if not isinstance(spec, ToolSpec)})
        if wrong:
            raise TypeError(
                "config['tools'] must contain contracts.tools.ToolSpec objects, got "
                + ", ".join(wrong)
            )

        self._events = events
        self._config = dict(config)
        self._language = config.get("transcription_language") or None
        self._model = config.get("model") or DEFAULT_MODEL
        self._effort = config.get("effort") or DEFAULT_EFFORT
        self._max_tokens = int(config.get("max_output_tokens") or DEFAULT_MAX_TOKENS)
        instructions = (config.get("instructions") or "").strip()
        self._system_prompt = f"{instructions}\n\n{LATENCY_INSTRUCTION}".strip()
        self._set_tools(tools)

        self._end_silence_ms = float(config.get("end_silence_ms", DEFAULT_END_SILENCE_MS))
        if self._stt is None:
            self._stt = build_stt(dict(config.get("stt") or {}), language=self._language)
        if self._tts is None:
            # The persona's `voice` names the TTS voice here, exactly as it
            # names the Realtime voice for the other provider.
            tts_config = dict(config.get("tts") or {})
            tts_config.setdefault("voice", config.get("voice") or "")
            self._tts = build_tts(tts_config, language=self._language)

        self._running = True
        self._ready.clear()
        self._worker = asyncio.create_task(self._speak_forever(), name="voice.claude.tts")
        self._warmup = asyncio.create_task(self._warm_up(), name="voice.claude.warmup")

    async def stop(self) -> None:
        self._running = False
        self._cancelled = True
        self._drain_queue()
        # Awaited, not just cancelled: a turn task left running would emit
        # `on_response_done` into a core that believes this provider is gone,
        # and would leave a `tool_use` in the history with no `tool_result`.
        pending = [*self._tasks, self._warmup, self._worker]
        self._tasks.clear()
        self._warmup = self._worker = None
        for task in pending:
            if task is not None:
                task.cancel()
        for task in pending:
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        client, self._client = self._client, None
        if self._owns_client and client is not None:
            with contextlib.suppress(Exception):
                await client.close()
        self._ready.clear()
        self._active_response_id = None
        self._current_item_id = None
        self._messages.clear()
        self._pending_system.clear()
        self._spoken.clear()
        self._tool_futures.clear()

    async def send_audio(self, pcm16: bytes) -> None:
        if not pcm16 or not self._running or self._turns is None:
            return
        transition = self._turns.feed(pcm16)
        if transition == SPEECH_STARTED:
            self._utterance = bytearray(b"".join(self._preroll))
            self._stamp("speech_started_ts")
            self._emit("on_speech_started")
        if self._utterance is not None:
            self._utterance.extend(pcm16)
        else:
            self._remember_preroll(pcm16)
        if transition == SPEECH_ENDED and self._utterance is not None:
            audio, self._utterance = bytes(self._utterance), None
            self._preroll.clear()
            self._preroll_bytes = 0
            self._stamp("speech_stopped_ts")
            self._emit("on_speech_ended")
            self._spawn(self._handle_utterance(audio))

    async def inject_system_text(self, text: str) -> None:
        """Queue a mid-conversation system message for the next turn.

        The API accepts ``{"role": "system"}`` inside ``messages`` -- which
        preserves the cached prefix -- but only after a user message, so an
        injection waits for the turn that gives it a legal slot rather than
        being dropped or promoted into the cached system prompt.
        """
        if text:
            self._pending_system.append(text)

    async def request_response(self, instructions: str | None = None) -> None:
        if self._active_response_id is not None:
            raise RuntimeError(
                f"request_response while response {self._active_response_id} is active"
            )
        self._spawn(self._run_turn(instruction=instructions))

    async def cancel_response(self, response_id: str) -> None:
        if not response_id:
            raise ValueError("cancel_response requires a response_id")
        if response_id != self._active_response_id:
            log.debug("cancel_response for %s, which is not the active response", response_id)
            return
        self._cancelled = True
        self._drain_queue()

    async def truncate_item(self, item_id: str, played_ms: float) -> None:
        """Cut the assistant turn in history down to what was really heard.

        There is no server-side item to truncate: what the model believes it
        said is the text in `_messages`, so it is trimmed by the proportion
        of the item's audio that actually reached the speaker. The budget is
        spent segment by segment, in the order they were spoken, so a turn
        that spoke twice around a tool call keeps its two messages apart.
        """
        if not item_id:
            raise ValueError("truncate_item requires an item_id")
        segments = self._spoken.get(item_id, ())
        total_ms = sum(segment.audio_ms for segment in segments)
        if total_ms <= 0:
            return
        budget = int(sum(len(segment.text) for segment in segments) * max(0.0, min(1.0, played_ms / total_ms)))
        for segment in segments:
            kept = segment.text[: max(0, budget)].rstrip()
            budget -= len(segment.text)
            segment.text = kept
            self._rewrite_text(segment.index, kept or "…")

    def _rewrite_text(self, index: int | None, text: str) -> None:
        """Replace the last text block of the assistant message at ``index``."""
        if index is None or index >= len(self._messages):
            return
        blocks = self._messages[index].get("content")
        if isinstance(blocks, str):
            self._messages[index]["content"] = text
            return
        for block in reversed(blocks or ()):
            if isinstance(block, dict) and block.get("type") == "text":
                block["text"] = text
                return

    async def send_tool_result(self, call_id: str, result: ToolResult) -> None:
        """Hand the real `ToolResult` to the turn that is waiting for it."""
        future = self._tool_futures.get(call_id)
        if future is None:
            log.warning("tool result for unknown call %s, dropped", call_id)
            return
        if not future.done():
            future.set_result(result)

    # ------------------------------------------- provider-specific surface

    @property
    def active_response_id(self) -> str | None:
        return self._active_response_id

    @property
    def current_item_id(self) -> str | None:
        return self._current_item_id

    @property
    def has_pending_tool_call(self) -> bool:
        return bool(self._tool_futures)

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def messages(self) -> list[dict[str, Any]]:
        """A copy of the conversation history sent on every request."""
        return [dict(message) for message in self._messages]

    def capabilities(self) -> dict[str, Any]:
        """What ``asimoov doctor`` prints about this pipeline."""
        stt_ok, stt_why = self._stt.available() if self._stt else (False, "not built")
        tts_ok, tts_why = self._tts.available() if self._tts else (False, "not built")
        return {
            "model": self._model,
            "effort": self._effort,
            "stt": {"engine": getattr(self._stt, "name", "none"), "ok": stt_ok, "reason": stt_why},
            "tts": {"engine": getattr(self._tts, "name", "none"), "ok": tts_ok, "reason": tts_why},
            "turns": self._turns.capabilities() if self._turns else {},
        }

    async def wait_ready(self, timeout_s: float = 10.0) -> None:
        """Wait until the STT and TTS engines are loaded.

        Raises:
            TimeoutError: if they are not ready within ``timeout_s``.
        """
        ready, _ = await run_with_timeout(self._ready.wait(), timeout_s)
        if not ready:
            raise asyncio.TimeoutError(f"the Claude pipeline was not ready within {timeout_s}s")

    async def update_tools(self, tools: Sequence[ToolSpec]) -> None:
        """Republish the tool list. This invalidates the prompt cache prefix."""
        self._config["tools"] = tuple(tools)
        self._set_tools(tools)

    async def request_summary(self, instructions: str, *, timeout_s: float = 15.0) -> str:
        """A text-only turn asking for a summary, kept out of the history.

        The turn lock is held throughout: `VoiceLoop.is_silent()` is already
        True while an utterance is being transcribed, so without it the
        summary would race a turn that is about to open.

        Raises:
            RuntimeError: if a turn is active (the caller waits for silence).
            TimeoutError: if the model does not answer within ``timeout_s``.
        """
        if self._active_response_id is not None:
            raise RuntimeError("request_summary while a turn is active")
        prompt = f"{instructions}\n{SUMMARY_SUFFIX}"
        async with self._turn_lock:
            messages = [*self._messages, {"role": "user", "content": prompt}]
            answered, message = await run_with_timeout(
                self._collect(messages, self._system_blocks()), timeout_s
            )
        if not answered:
            raise asyncio.TimeoutError(f"no summary within {timeout_s}s")
        if message is None or getattr(message, "stop_reason", None) == "refusal":
            return ""
        return "".join(
            block.text for block in message.content if getattr(block, "type", "") == "text"
        ).strip()

    async def prune_items(self, max_items: int = 40) -> list[str]:
        """Drop the oldest messages beyond ``max_items``.

        The cut never lands on a ``tool_result``: a history that starts with
        one, or whose ``tool_use`` lost its answer, is rejected by the API.
        """
        if self._turn_lock.locked() or len(self._messages) <= max_items:
            return []
        drop = len(self._messages) - max_items
        while drop < len(self._messages) and not self._starts_a_turn(drop):
            drop += 1
        if drop >= len(self._messages):
            return []
        self._messages = self._messages[drop:]
        if self._system_index is not None:
            self._system_index -= drop
        for segments in self._spoken.values():
            for segment in segments:
                segment.index = (
                    segment.index - drop
                    if segment.index is not None and segment.index >= drop
                    else None
                )
        return [f"msg_{index}" for index in range(drop)]

    # ---------------------------------------------------------- internals

    def _set_tools(self, tools: Sequence[ToolSpec]) -> None:
        self._tools = tuple(tools)
        self._tools_by_name = {spec.name: spec for spec in self._tools}

    def _starts_a_turn(self, index: int) -> bool:
        message = self._messages[index]
        if message.get("role") != "user":
            return False
        content = message.get("content")
        if isinstance(content, list):
            return not any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in content
            )
        return True

    def _remember_preroll(self, pcm16: bytes) -> None:
        budget = int(self.sample_rate_hz * 2 * PREROLL_MS / 1000.0)
        self._preroll.append(pcm16)
        self._preroll_bytes += len(pcm16)
        while self._preroll_bytes > budget and len(self._preroll) > 1:
            self._preroll_bytes -= len(self._preroll.popleft())

    def _require_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise RuntimeError(
                "the anthropic SDK is not installed (pip install 'asimoov[claude]')"
            ) from exc
        if not self._api_key:
            from asimoov.core.config import Secrets

            self._api_key = Secrets().require(self.secret_key)
        self._client = AsyncAnthropic(api_key=self._api_key)
        return self._client

    def _stream(self, **kwargs: Any) -> Any:
        """Open a streaming request, with server-side refusal fallbacks on.

        ``fallbacks="default"`` lets the API re-run a declined request on the
        substitute Anthropic recommends for that refusal category instead of
        handing a mute robot back to the user.
        """
        client = self._require_client()
        beta = getattr(client, "beta", None) if self._fallbacks else None
        if beta is not None:
            return beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        return client.messages.stream(**kwargs)

    def _request(
        self, messages: list[dict[str, Any]], system: list[dict[str, Any]]
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self._effort},
        }
        if self._tools:
            payload["tools"] = [tool_to_claude(spec) for spec in self._tools]
        return payload

    def _system_blocks(self, *extras: str | None) -> list[dict[str, Any]]:
        """The cached persona prompt first, everything volatile after it."""
        blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": self._system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        blocks.extend({"type": "text", "text": extra} for extra in extras if extra)
        return blocks

    def _flush_pending(self) -> str | None:
        """Place the queued injections, or hand them to this request only.

        ``{"role": "system"}`` inside ``messages`` keeps the cached prefix
        intact, but the API only accepts it right after a ``user`` turn (and
        only if an assistant turn follows, which `_withdraw_system` enforces
        after the fact). A turn the robot starts by itself does not follow
        one, so there the text rides as a system block instead of poisoning
        the history for every request that comes after.
        """
        if not self._pending_system:
            return None
        text = "\n".join(self._pending_system)
        self._pending_system.clear()
        if self._messages and self._messages[-1].get("role") == "user":
            self._messages.append({"role": "system", "content": text})
            self._system_index = len(self._messages) - 1
            return None
        return text

    def _withdraw_system(self) -> None:
        """Take the injection back when the turn produced no assistant message.

        A ``system`` message must be the last entry or be followed by an
        assistant turn. A refusal, a barge-in before the first word or an
        unparsable tool input all end a turn without one, and a dangling
        ``system`` would make every later request a 400.
        """
        index, self._system_index = self._system_index, None
        if index is None or index != len(self._messages) - 1:
            return
        self._pending_system.insert(0, str(self._messages.pop(index)["content"]))

    def _open_conversation(self) -> None:
        """Never send an empty ``messages``: the API rejects it."""
        if not self._messages:
            self._messages.append({"role": "user", "content": OPENING_SEED})

    async def _warm_up(self) -> None:
        """Load the STT and TTS models now, so the first turn does not.

        An engine that cannot run is an error, not a warning: the robot is
        deaf or mute for the whole session and ``asimoov doctor`` says which.
        Loading still continues, so the other half of the pipeline works.
        The turn detector is built here too: `SileroVad` opens an ONNX
        session, which is well over the 100 ms `VoiceProvider.start` is
        allowed to block for.
        """
        if self._turns is None:
            self._turns = TurnDetector(
                config=TurnConfig(end_silence_ms=self._end_silence_ms),
                sample_rate_hz=self.sample_rate_hz,
            )
        for label, engine in (("stt", self._stt), ("tts", self._tts)):
            if engine is None:
                continue
            usable, reason = engine.available()
            if not usable:
                log.error("%s engine %s is unusable: %s", label, engine.name, reason)
                continue
            warm = getattr(engine, "warm_up", None)
            if warm is None:
                continue
            try:
                await warm()
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                log.error("%s engine %s failed to load: %s", label, engine.name, exc)
        self._ready.set()

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _emit(self, name: str, *args: Any) -> None:
        events = self._events
        if events is None:
            return
        try:
            getattr(events, name)(*args)
        except Exception:
            log.exception("VoiceEvents.%s raised", name)

    def _emit_optional(self, name: str, *args: Any) -> None:
        if hasattr(self._events, name):
            self._emit(name, *args)

    def _stamp(self, name: str) -> None:
        if self._on_timestamp is not None:
            self._on_timestamp(name, time.time())

    async def _handle_utterance(self, audio: bytes) -> None:
        assert self._stt is not None
        try:
            text = await self._stt.transcribe(
                audio, sample_rate_hz=self.sample_rate_hz, language=self._language
            )
        except Exception:
            log.exception("transcription failed, the utterance is lost")
            return
        if not text.strip():
            return
        self._emit("on_utterance", Utterance(text=text, lang=self._language or "und", final=True))
        await self._run_turn(user_text=text)

    # -- one turn ---------------------------------------------------------

    async def _run_turn(
        self, *, user_text: str | None = None, instruction: str | None = None
    ) -> None:
        # Queued, never dropped: the microphone stays open while the model
        # thinks, so a second sentence during a turn is a normal thing to
        # hear and losing it would be a silent failure.
        async with self._turn_lock:
            if not self._running:
                return
            await self._turn(user_text=user_text, instruction=instruction)

    async def _turn(self, *, user_text: str | None, instruction: str | None) -> None:
        if user_text is not None:
            self._messages.append({"role": "user", "content": user_text})
        self._open_conversation()
        injected = self._flush_pending()

        response_id = f"resp_{next(self._sequence)}"
        item_id = f"item_{response_id}"
        self._active_response_id = response_id
        self._current_item_id = item_id
        self._cancelled = False
        self._say_buffer = ""
        self._round = -1
        self._forget_old_items()
        self._spoken[item_id] = []
        self._emit_optional("on_response_started", response_id)
        try:
            await self._converse(item_id, injected, instruction)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("the Claude turn failed")
        finally:
            await self._finish(response_id, item_id)

    async def _converse(
        self, item_id: str, injected: str | None, instruction: str | None
    ) -> None:
        for _round in range(MAX_TOOL_ROUNDS):
            system = self._system_blocks(injected, instruction)
            injected = instruction = None
            self._spoken[item_id].append(_Segment())
            self._round = len(self._spoken[item_id]) - 1
            message = await self._stream_once(item_id, system)
            if message is None or self._cancelled:
                return
            # The refusal is read before the content: a decline can cut a
            # tool_use off mid-input, and running it would be running half a
            # call the model never finished asking for.
            if getattr(message, "stop_reason", None) == "refusal":
                details = getattr(message, "stop_details", None)
                log.warning("Claude refused this turn (%s)", getattr(details, "category", None))
                return
            tool_uses = [
                block for block in message.content if getattr(block, "type", "") == "tool_use"
            ]
            if message.stop_reason == "max_tokens" and tool_uses:
                log.error("tool input truncated at max_tokens; the call is not run")
                return
            self._remember_assistant(message, item_id)
            if not tool_uses:
                return
            self._messages.append({"role": "user", "content": await self._dispatch(tool_uses)})
        log.warning("stopped after %d tool rounds in a single turn", MAX_TOOL_ROUNDS)

    async def _stream_once(self, item_id: str, system: list[dict[str, Any]]) -> Any:
        payload = self._request(list(self._messages), system)
        try:
            async with self._stream(**payload) as stream:
                async for event in stream:
                    if self._cancelled:
                        return None
                    if getattr(event, "type", "") == "text":
                        self._feed_text(item_id, event.text)
                if self._cancelled:
                    return None
                message = await stream.get_final_message()
        except ValueError:
            # Tool JSON the SDK could not parse at all. It raised before the
            # block completed, so there is no tool_use_id to answer with.
            log.error("unparsable tool input on the stream, the turn is dropped")
            return None
        self._flush_text(item_id)
        self._note_usage(message)
        return message

    async def _collect(
        self, messages: list[dict[str, Any]], system: list[dict[str, Any]]
    ) -> Any:
        """One request that is never spoken (the renewal summary), text only."""
        payload = self._request(messages, system)
        payload.pop("tools", None)
        async with self._stream(**payload) as stream:
            async for _event in stream:
                pass
            return await stream.get_final_message()

    def _note_usage(self, message: Any) -> None:
        usage = getattr(message, "usage", None)
        if usage is None or self._on_usage is None:
            return
        payload = {
            field: int(getattr(usage, field, 0) or 0)
            for field in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        }
        payload["total_tokens"] = sum(payload.values())
        self._on_usage(payload)

    def _remember_assistant(self, message: Any, item_id: str) -> None:
        content = [_block_to_dict(block) for block in message.content]
        if not content:
            return
        self._messages.append({"role": "assistant", "content": content})
        if any(block.get("type") == "text" for block in content):
            self._spoken[item_id][self._round].index = len(self._messages) - 1

    async def _dispatch(self, tool_uses: Sequence[Any]) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        results: list[dict[str, Any]] = []
        waiting: list[tuple[int, str, float]] = []
        for block in tool_uses:
            spec = self._tools_by_name.get(block.name)
            if spec is None or not _valid_input(spec.params, block.input):
                log.error("tool %s called with invalid arguments", block.name)
                results.append(
                    self._tool_result(
                        block.id,
                        ToolResult(status=BehaviorStatus.ERROR, reason="invalid_arguments"),
                    )
                )
                continue
            self._tool_futures[block.id] = loop.create_future()
            waiting.append((len(results), block.id, spec.timeout_s))
            results.append({})
            self._emit("on_tool_call", block.name, dict(block.input), block.id)
        for index, call_id, timeout_s in waiting:
            future = self._tool_futures[call_id]
            answered, value = await run_with_timeout(future, timeout_s + TOOL_RESULT_MARGIN_S)
            self._tool_futures.pop(call_id, None)
            result = (
                value
                if answered
                else ToolResult(status=BehaviorStatus.TIMEOUT, reason="no tool result")
            )
            results[index] = self._tool_result(call_id, result)
        return results

    @staticmethod
    def _tool_result(call_id: str, result: ToolResult) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": call_id,
            "content": json.dumps(result.to_dict(), ensure_ascii=False),
            # A timeout or an unsupported tool is a failure too: reported as
            # a success, the model would build its next sentence on it.
            "is_error": result.status is not BehaviorStatus.OK,
        }

    # -- speaking ---------------------------------------------------------

    def _feed_text(self, item_id: str, delta: str) -> None:
        self._say_buffer += delta
        while True:
            cut = self._sentence_cut(self._say_buffer)
            if cut is None:
                return
            chunk, self._say_buffer = self._say_buffer[:cut], self._say_buffer[cut:]
            self._queue.put_nowait((item_id, self._round, chunk))

    @staticmethod
    def _sentence_cut(buffer: str) -> int | None:
        """Where to cut for the next TTS chunk, or None while it is too short."""
        for index, character in enumerate(buffer):
            if character in SENTENCE_END and len(buffer[: index + 1].strip()) >= MIN_CHUNK_CHARS:
                return index + 1
        return None

    def _flush_text(self, item_id: str) -> None:
        text, self._say_buffer = self._say_buffer, ""
        if text.strip():
            self._queue.put_nowait((item_id, self._round, text))

    async def _speak_forever(self) -> None:
        assert self._tts is not None
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                item_id, round_index, text = item
                if self._cancelled:
                    continue
                try:
                    pcm16 = await self._tts.synthesize(text, sample_rate_hz=self.sample_rate_hz)
                except Exception:
                    log.exception("speech synthesis failed for one chunk")
                    continue
                if self._cancelled or not pcm16:
                    continue
                # The round travelled with the chunk, so a late synthesis is
                # still credited to the message that asked for it.
                segment = self._spoken[item_id][round_index]
                segment.text += text
                segment.audio_ms += len(pcm16) / 2 * 1000.0 / self.sample_rate_hz
                self._emit("on_audio_out", item_id, pcm16)
            finally:
                self._queue.task_done()

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._queue.task_done()

    async def _finish(self, response_id: str, item_id: str) -> None:
        if self._cancelled:
            self._say_buffer = ""
            self._drain_queue()
        else:
            self._flush_text(item_id)
            await self._queue.join()
        self._remember_partial(item_id)
        self._withdraw_system()
        for call_id, future in list(self._tool_futures.items()):
            future.cancel()
            self._tool_futures.pop(call_id, None)
        spoken = "".join(segment.text for segment in self._spoken.get(item_id, ()))
        if spoken:
            if self._on_assistant_text is not None:
                self._on_assistant_text(spoken)
            self._emit_optional("on_assistant_text", spoken)
        self._active_response_id = None
        self._emit("on_response_done", response_id)

    def _forget_old_items(self) -> None:
        """Only a recent item can still be truncated; the rest is dead weight."""
        for stale in list(self._spoken)[:-KEPT_ITEMS]:
            self._spoken.pop(stale, None)

    def _remember_partial(self, item_id: str) -> None:
        """A turn cut short still said something: keep exactly that in history.

        Without it the assistant message is missing and a `system` injection
        right before it would be left dangling, which the API rejects.
        """
        for segment in self._spoken.get(item_id, ()):
            if segment.index is not None or not segment.text.strip():
                continue
            self._messages.append(
                {"role": "assistant", "content": [{"type": "text", "text": segment.text.strip()}]}
            )
            segment.index = len(self._messages) - 1


__all__ = ["LATENCY_INSTRUCTION", "ClaudePipelineProvider", "tool_to_claude"]
