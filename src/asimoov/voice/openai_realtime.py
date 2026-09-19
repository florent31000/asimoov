"""OpenAI Realtime `VoiceProvider`, over asyncio `websockets`.

The wire format follows the GA `session` shape (``type: realtime``,
``output_modalities``, ``audio.input`` / ``audio.output``) that Neon validated
against the live API. The behavioural differences with Neon are deliberate and
listed in `docs/voice.md`: the receive loop is a task on the single asyncio
loop (no thread), `cancel_response` always carries a ``response_id``, tool
results carry the real `ToolResult`, ``response.create`` after a tool result is
only sent when no response is active, and there is no transcript keyword filter.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import time
from collections.abc import Callable, Sequence
from typing import Any

from asimoov.contracts.percepts import Utterance
from asimoov.contracts.tools import ToolResult, ToolSpec
from asimoov.contracts.voice import VoiceEvents, VoiceProvider
from asimoov.core.timeouts import run_with_timeout

try:  # websockets >= 13
    from websockets.asyncio.client import connect as _ws_connect

    _HEADERS_KW = "additional_headers"
except ImportError:  # websockets 12
    from websockets.client import connect as _ws_connect  # type: ignore[no-redef]

    _HEADERS_KW = "extra_headers"

log = logging.getLogger(__name__)

DEFAULT_URL = "wss://api.openai.com/v1/realtime"
DEFAULT_MODEL = "gpt-realtime-1.5"
DEFAULT_VOICE = "alloy"
DEFAULT_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"
TURN_DETECTION_TYPES = ("server_vad", "semantic_vad")
NOISE_REDUCTION_TYPES = ("near_field", "far_field")
OOB_METADATA_KEY = "asimoov_oob"


class NotConnected(RuntimeError):
    """Raised when a message is sent while the socket is down."""


def tool_to_realtime(spec: ToolSpec) -> dict[str, Any]:
    """Convert a frozen `ToolSpec` into a Realtime function tool definition."""
    return {
        "type": "function",
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.params,
    }


def build_session(config: dict[str, Any], *, sample_rate_hz: int) -> dict[str, Any]:
    """Build the ``session`` payload of ``session.update`` from a voice config.

    Raises:
        TypeError: if ``tools`` is not a sequence of `ToolSpec`. Passing
            already-serialized dicts is a programming error on the caller's
            side and must surface at once, not as an endless reconnect loop.
        ValueError: on an unknown ``turn_detection`` or ``noise_reduction``.
    """
    turn_type = config.get("turn_detection", "server_vad")
    if turn_type not in TURN_DETECTION_TYPES:
        raise ValueError(f"invalid turn_detection: {turn_type!r}")
    turn_detection: dict[str, Any] = {"type": turn_type}
    if turn_type == "server_vad":
        turn_detection["threshold"] = config.get("vad_threshold", 0.5)
        turn_detection["silence_duration_ms"] = config.get("silence_duration_ms", 600)
    else:
        turn_detection["eagerness"] = config.get("eagerness", "auto")

    audio_input: dict[str, Any] = {
        "format": {"type": "audio/pcm", "rate": sample_rate_hz},
        "turn_detection": turn_detection,
    }
    noise_reduction = config.get("noise_reduction", "near_field")
    if noise_reduction is not None:
        if noise_reduction not in NOISE_REDUCTION_TYPES:
            raise ValueError(f"invalid noise_reduction: {noise_reduction!r}")
        audio_input["noise_reduction"] = {"type": noise_reduction}
    transcription_model = config.get("transcription_model", DEFAULT_TRANSCRIPTION_MODEL)
    if transcription_model:
        transcription: dict[str, Any] = {"model": transcription_model}
        language = config.get("transcription_language")
        if language:
            transcription["language"] = language
        audio_input["transcription"] = transcription

    tools: Sequence[ToolSpec] = config.get("tools", ())
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
    session: dict[str, Any] = {
        "type": "realtime",
        "output_modalities": ["audio"],
        "instructions": config.get("instructions", ""),
        "audio": {
            "input": audio_input,
            "output": {
                "format": {"type": "audio/pcm", "rate": sample_rate_hz},
                "voice": config.get("voice", DEFAULT_VOICE),
            },
        },
        "tools": [tool_to_realtime(spec) for spec in tools],
        "tool_choice": config.get("tool_choice", "auto"),
    }
    max_output_tokens = config.get("max_output_tokens")
    if max_output_tokens is not None:
        session["max_output_tokens"] = max_output_tokens
    return session


class OpenAIRealtimeProvider(VoiceProvider):
    """Speech-to-speech provider backed by the OpenAI Realtime API.

    Args:
        api_key: Bearer token. Defaults to ``$OPENAI_API_KEY``. Required for
            ``wss://`` endpoints; a ``ws://`` endpoint (the test server) may
            run without one.
        url: Realtime endpoint, without the ``model`` query parameter.
        on_timestamp: Telemetry hook, called as ``(name, unix_seconds)`` for
            ``speech_started_ts``, ``speech_stopped_ts`` and
            ``first_audio_delta_ts`` (plan.md section 4.11).
        on_assistant_text: Called with the assistant transcript of a finished
            response. `VoiceEvents` carries no role, so this stays a plain
            callable rather than a fake `Utterance`.
        on_usage: Called with the ``usage`` dict of ``response.done``; wired to
            `session.SessionManager.note_usage` by the core.
    """

    sample_rate_hz = 24000

    def __init__(
        self,
        api_key: str | None = None,
        *,
        url: str = DEFAULT_URL,
        on_timestamp: Callable[[str, float], None] | None = None,
        on_assistant_text: Callable[[str], None] | None = None,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
        max_backoff_s: float = 8.0,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        self._url = url
        self._on_timestamp = on_timestamp
        self._on_assistant_text = on_assistant_text
        self._on_usage = on_usage
        self._max_backoff_s = max_backoff_s

        self._events: VoiceEvents | None = None
        self._config: dict[str, Any] = {}
        self._session: dict[str, Any] = {}
        self._ws: Any = None
        self._task: asyncio.Task[None] | None = None
        self._bg_tasks: set[asyncio.Task[None]] = set()
        self._running = False
        self._ready = asyncio.Event()

        self._active_response_id: str | None = None
        self._current_item_id: str | None = None
        self._pending_tool_calls: set[str] = set()
        self._item_ids: list[str] = []
        self._system_item_id: str | None = None
        self._assistant_text: list[str] = []
        self._oob_future: asyncio.Future[str] | None = None
        self._oob_response_id: str | None = None
        self._oob_tag: str | None = None
        self._oob_text: list[str] = []
        self._first_delta_seen = False
        self._dropped_audio_chunks = 0

    # ---------------------------------------------------------------- ABC

    async def start(self, events: VoiceEvents, config: dict[str, Any]) -> None:
        if self._running:
            raise RuntimeError("provider already started")
        if not self._api_key and self._url.startswith("wss://"):
            raise ValueError("OpenAI Realtime requires an API key (set OPENAI_API_KEY)")
        self._events = events
        self._config = dict(config)
        # Built here, in the caller's frame, rather than inside `_run`: a
        # malformed session payload is a programming error and reconnecting
        # forever would only turn it into a mute robot (review, blocker 1).
        self._session = build_session(self._config, sample_rate_hz=self.sample_rate_hz)
        self._running = True
        self._ready.clear()
        self._task = asyncio.create_task(self._run(), name="voice.openai_realtime")

    async def stop(self) -> None:
        self._running = False
        task, self._task = self._task, None
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception as exc:  # the socket is going away anyway
                log.debug("error closing realtime socket: %s", exc)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._ready.clear()
        self._reset_session_state()

    async def send_audio(self, pcm16: bytes) -> None:
        if not pcm16:
            return
        payload = {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm16).decode("ascii"),
        }
        try:
            await self._send(payload)
        except NotConnected:
            # Mic audio is disposable while the socket reconnects; the count is
            # exposed as `dropped_audio_chunks` and logged by the reconnect path.
            self._dropped_audio_chunks += 1

    async def inject_system_text(self, text: str) -> None:
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )

    async def request_response(self, instructions: str | None = None) -> None:
        if self._active_response_id is not None:
            raise RuntimeError(
                f"request_response while response {self._active_response_id} is active"
            )
        payload: dict[str, Any] = {"type": "response.create"}
        if instructions:
            payload["response"] = {"instructions": instructions}
        await self._send(payload)

    async def cancel_response(self, response_id: str) -> None:
        if not response_id:
            raise ValueError("cancel_response requires a response_id")
        await self._send({"type": "response.cancel", "response_id": response_id})

    async def truncate_item(self, item_id: str, played_ms: float) -> None:
        if not item_id:
            raise ValueError("truncate_item requires an item_id")
        await self._send(
            {
                "type": "conversation.item.truncate",
                "item_id": item_id,
                "content_index": 0,
                "audio_end_ms": max(0, int(played_ms)),
            }
        )

    # ------------------------------------------- provider-specific surface

    @property
    def active_response_id(self) -> str | None:
        """Id of the response currently streaming, or None."""
        return self._active_response_id

    @property
    def current_item_id(self) -> str | None:
        """Id of the assistant item whose audio is streaming, or None."""
        return self._current_item_id

    @property
    def has_pending_tool_call(self) -> bool:
        """True while a local function call is executing (barge-in skips the cancel)."""
        return bool(self._pending_tool_calls)

    @property
    def item_ids(self) -> list[str]:
        return list(self._item_ids)

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def dropped_audio_chunks(self) -> int:
        """Mic chunks dropped because the socket was down (reconnect windows)."""
        return self._dropped_audio_chunks

    async def wait_ready(self, timeout_s: float = 10.0) -> None:
        """Wait until ``session.created`` arrived.

        Raises:
            TimeoutError: if the session is not ready within ``timeout_s``.
        """
        ready, _ = await run_with_timeout(self._ready.wait(), timeout_s)
        if not ready:
            raise TimeoutError(f"the realtime session was not ready within {timeout_s}s")

    async def send_tool_result(self, call_id: str, result: ToolResult) -> None:
        """Send the REAL tool result, then ``response.create`` only if idle.

        Neon sent ``str(result)`` (often the literal ``"ok"``) and always
        followed with ``response.create``, racing the response that was still
        streaming (plan.md section 2, item 7).
        """
        self._pending_tool_calls.discard(call_id)
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result.to_dict(), ensure_ascii=False),
                },
            }
        )
        if self._active_response_id is None:
            await self._send({"type": "response.create"})

    async def request_summary(self, instructions: str, *, timeout_s: float = 15.0) -> str:
        """Ask for an out-of-band text summary (``conversation: none``).

        The summary never enters the conversation, so it does not grow the
        context it exists to replace.

        Raises:
            RuntimeError: if a summary is already in flight.
            TimeoutError: if the model does not answer within ``timeout_s``.
        """
        if self._oob_future is not None and not self._oob_future.done():
            raise RuntimeError("a summary request is already in flight")
        loop = asyncio.get_running_loop()
        self._oob_future = loop.create_future()
        self._oob_response_id = None
        # Attribution by a field the API echoes back, never by arrival order:
        # the model starts its own responses and one of them can land between
        # our `response.create` and its `response.created` (review, medium).
        self._oob_tag = f"oob_{id(self._oob_future):x}"
        self._oob_text = []
        await self._send(
            {
                "type": "response.create",
                "response": {
                    "conversation": "none",
                    "output_modalities": ["text"],
                    "instructions": instructions,
                    "metadata": {OOB_METADATA_KEY: self._oob_tag},
                },
            }
        )
        try:
            answered, summary = await run_with_timeout(self._oob_future, timeout_s)
        finally:
            self._oob_future = None
            self._oob_response_id = None
            self._oob_tag = None
        if not answered:
            raise TimeoutError(f"no summary within {timeout_s}s")
        return summary

    async def update_tools(self, tools: Sequence[ToolSpec]) -> None:
        """Republish the tool list on the live session.

        A body that only learns its capabilities once its link is up changes
        which behaviors exist, and the model must be told (review, major 11).
        """
        self._config["tools"] = tuple(tools)
        self._session = build_session(self._config, sample_rate_hz=self.sample_rate_hz)
        if self._ws is not None:
            await self._send({"type": "session.update", "session": self._session})

    async def delete_item(self, item_id: str) -> None:
        await self._send({"type": "conversation.item.delete", "item_id": item_id})

    async def prune_items(self, max_items: int = 40) -> list[str]:
        """Delete the oldest items beyond ``max_items``, keeping the first system item.

        Returns the ids actually deleted.
        """
        if len(self._item_ids) <= max_items:
            return []
        keep: list[str] = []
        if self._system_item_id is not None:
            keep.append(self._system_item_id)
        room = max(0, max_items - len(keep))
        tail = [item for item in self._item_ids if item not in keep][-room:] if room else []
        keep.extend(tail)
        doomed = [item for item in self._item_ids if item not in keep]
        for item_id in doomed:
            await self.delete_item(item_id)
        return doomed

    # ---------------------------------------------------------- internals

    def _lang(self) -> str:
        return self._config.get("transcription_language") or "und"

    def _reset_session_state(self) -> None:
        self._active_response_id = None
        self._current_item_id = None
        self._pending_tool_calls.clear()
        self._item_ids.clear()
        self._system_item_id = None
        self._assistant_text = []
        self._first_delta_seen = False

    async def _send(self, payload: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise NotConnected(f"realtime socket is down, dropped {payload['type']}")
        await ws.send(json.dumps(payload))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def _endpoint(self) -> str:
        model = self._config.get("model", DEFAULT_MODEL)
        separator = "&" if "?" in self._url else "?"
        return f"{self._url}{separator}model={model}"

    async def _run(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                kwargs: dict[str, Any] = {}
                headers = self._headers()
                if headers:
                    kwargs[_HEADERS_KW] = headers
                async with _ws_connect(self._endpoint(), **kwargs) as ws:
                    self._ws = ws
                    backoff = 1.0
                    await self._send({"type": "session.update", "session": self._session})
                    async for raw in ws:
                        self._dispatch(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("realtime session ended (%s: %s)", type(exc).__name__, exc)
            finally:
                self._ws = None
                self._ready.clear()
                self._reset_session_state()
            if not self._running:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff_s)

    def _emit(self, name: str, *args: Any) -> None:
        events = self._events
        if events is None:
            return
        try:
            getattr(events, name)(*args)
        except Exception:
            # A raising core callback must not take the socket down with it; it
            # is logged with its traceback, never swallowed silently.
            log.exception("VoiceEvents.%s raised", name)

    def _emit_optional(self, name: str, *args: Any) -> None:
        """Call an optional `VoiceEvents` hook only if the core implements it."""
        if hasattr(self._events, name):
            self._emit(name, *args)

    def _stamp(self, name: str) -> None:
        if self._on_timestamp is not None:
            self._on_timestamp(name, time.time())

    def _dispatch(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")
        handler = _HANDLERS.get(etype)
        if handler is None:
            if etype == "error":
                log.error("realtime error: %s", event.get("error", event))
            else:
                log.debug("unhandled realtime event: %s", etype)
            return
        handler(self, event)

    def _on_session_created(self, event: dict[str, Any]) -> None:
        self._ready.set()
        log.info("realtime session created")

    def _on_item_created(self, event: dict[str, Any]) -> None:
        item = event.get("item", {})
        item_id = item.get("id")
        if not item_id:
            return
        if item_id not in self._item_ids:
            self._item_ids.append(item_id)
        if self._system_item_id is None and item.get("role") == "system":
            self._system_item_id = item_id

    def _on_item_deleted(self, event: dict[str, Any]) -> None:
        item_id = event.get("item_id")
        if item_id in self._item_ids:
            self._item_ids.remove(item_id)

    def _on_speech_started(self, event: dict[str, Any]) -> None:
        self._stamp("speech_started_ts")
        self._emit("on_speech_started")

    def _on_speech_stopped(self, event: dict[str, Any]) -> None:
        self._stamp("speech_stopped_ts")
        self._emit("on_speech_ended")

    def _on_transcription_delta(self, event: dict[str, Any]) -> None:
        text = event.get("delta", "")
        if text:
            self._emit("on_utterance", Utterance(text=text, lang=self._lang(), final=False))

    def _on_transcription_completed(self, event: dict[str, Any]) -> None:
        text = event.get("transcript", "")
        if text:
            self._emit("on_utterance", Utterance(text=text, lang=self._lang(), final=True))

    def _on_transcription_failed(self, event: dict[str, Any]) -> None:
        log.warning("input transcription failed: %s", event.get("error", event))

    def _is_oob(self, response: dict[str, Any]) -> bool:
        """True if ``response`` is the out-of-band summary we asked for.

        Matched on the ``metadata`` tag the API echoes back, with the
        out-of-band shape (``conversation: none``) as the only other
        evidence -- never on the order the events happened to arrive in.
        """
        if self._oob_tag is None:
            return False
        metadata = response.get("metadata") or {}
        if isinstance(metadata, dict) and metadata.get(OOB_METADATA_KEY) == self._oob_tag:
            return True
        response_id = response.get("id")
        if response_id and response_id == self._oob_response_id:
            return True
        return self._oob_response_id is None and response.get("conversation") == "none"

    def _on_response_created(self, event: dict[str, Any]) -> None:
        response = event.get("response", {})
        response_id = response.get("id")
        if not response_id:
            return
        if self._is_oob(response):
            self._oob_response_id = response_id
            return
        self._active_response_id = response_id
        self._first_delta_seen = False
        # v1.3: the model starts most responses itself. Without this the core
        # believes it is idle and injects straight into the robot's speech
        # (review, blocker 4).
        self._emit_optional("on_response_started", response_id)

    def _on_audio_delta(self, event: dict[str, Any]) -> None:
        delta = event.get("delta", "")
        if not delta:
            return
        item_id = event.get("item_id", "")
        self._current_item_id = item_id
        if not self._first_delta_seen:
            self._first_delta_seen = True
            self._stamp("first_audio_delta_ts")
        self._emit("on_audio_out", item_id, base64.b64decode(delta))

    def _on_audio_transcript_delta(self, event: dict[str, Any]) -> None:
        self._assistant_text.append(event.get("delta", ""))

    def _on_audio_transcript_done(self, event: dict[str, Any]) -> None:
        text = event.get("transcript") or "".join(self._assistant_text)
        self._assistant_text = []
        if not text:
            return
        if self._on_assistant_text is not None:
            self._on_assistant_text(text)
        # v1.2: the same text through the optional `VoiceEvents` hook, which
        # is how the core (which builds this provider with no arguments)
        # journals what the robot said.
        self._emit_optional("on_assistant_text", text)

    def _on_text_delta(self, event: dict[str, Any]) -> None:
        if event.get("response_id") == self._oob_response_id:
            self._oob_text.append(event.get("delta", ""))

    def _on_text_done(self, event: dict[str, Any]) -> None:
        if event.get("response_id") == self._oob_response_id:
            self._oob_text = [event.get("text") or "".join(self._oob_text)]

    def _on_function_call_done(self, event: dict[str, Any]) -> None:
        call_id = event.get("call_id", "")
        name = event.get("name", "")
        raw_args = event.get("arguments", "{}")
        try:
            params = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            log.error("tool %s: unparsable arguments %r", name, raw_args)
            self._spawn(
                self.send_tool_result(call_id, ToolResult(status="error", reason="invalid_arguments"))
            )
            return
        self._pending_tool_calls.add(call_id)
        self._emit("on_tool_call", name, params, call_id)

    def _on_response_done(self, event: dict[str, Any]) -> None:
        response = event.get("response", {})
        response_id = response.get("id", "")
        usage = response.get("usage")
        if usage and self._on_usage is not None:
            self._on_usage(usage)
        if self._is_oob(response):
            future = self._oob_future
            if future is not None and not future.done():
                future.set_result("".join(self._oob_text).strip())
            self._oob_text = []
            return
        if response_id == self._active_response_id:
            self._active_response_id = None
        self._emit("on_response_done", response_id)


_HANDLERS: dict[str, Callable[[OpenAIRealtimeProvider, dict[str, Any]], None]] = {
    "session.created": OpenAIRealtimeProvider._on_session_created,
    "conversation.item.created": OpenAIRealtimeProvider._on_item_created,
    "conversation.item.added": OpenAIRealtimeProvider._on_item_created,
    "conversation.item.deleted": OpenAIRealtimeProvider._on_item_deleted,
    "input_audio_buffer.speech_started": OpenAIRealtimeProvider._on_speech_started,
    "input_audio_buffer.speech_stopped": OpenAIRealtimeProvider._on_speech_stopped,
    "conversation.item.input_audio_transcription.delta": (
        OpenAIRealtimeProvider._on_transcription_delta
    ),
    "conversation.item.input_audio_transcription.completed": (
        OpenAIRealtimeProvider._on_transcription_completed
    ),
    "conversation.item.input_audio_transcription.failed": (
        OpenAIRealtimeProvider._on_transcription_failed
    ),
    "response.created": OpenAIRealtimeProvider._on_response_created,
    "response.output_audio.delta": OpenAIRealtimeProvider._on_audio_delta,
    "response.output_audio_transcript.delta": OpenAIRealtimeProvider._on_audio_transcript_delta,
    "response.output_audio_transcript.done": OpenAIRealtimeProvider._on_audio_transcript_done,
    "response.output_text.delta": OpenAIRealtimeProvider._on_text_delta,
    "response.output_text.done": OpenAIRealtimeProvider._on_text_done,
    "response.function_call_arguments.done": OpenAIRealtimeProvider._on_function_call_done,
    "response.done": OpenAIRealtimeProvider._on_response_done,
}
