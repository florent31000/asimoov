"""A local Realtime server: replays scripted events, records everything sent.

No network, no API key: `websockets.serve` on 127.0.0.1 with an ephemeral port,
answering the subset of the protocol the provider uses.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import time
from typing import Any

_SEQ = itertools.count()

try:  # websockets >= 13
    from websockets.asyncio.server import serve as _ws_serve
except ImportError:  # pragma: no cover - websockets 12
    from websockets.server import serve as _ws_serve  # type: ignore[no-redef]


class FakeConnection:
    """One client socket: records inbound events, emits outbound ones."""

    def __init__(self, ws: Any, index: int) -> None:
        self._ws = ws
        self.index = index
        self.received: list[dict[str, Any]] = []
        self.session: dict[str, Any] | None = None
        self.session_updated_at: float | None = None
        self.session_updated_seq: int | None = None
        self.closed_at: float | None = None
        self.closed_seq: int | None = None
        self.audio_chunks: list[bytes] = []
        self.truncations: list[dict[str, Any]] = []
        self.canceled: list[dict[str, Any]] = []
        self.deleted_items: list[str] = []
        self.summary_text = "Sam asked for a dance; the robot danced and said it was tired."
        self.auto_respond = False
        self.active_response_id: str | None = None
        self.rejected_responses = 0
        self.interleave_model_response = False
        self._items = 0
        self._responses = 0

    # --------------------------------------------------------- outbound

    async def send(self, event: dict[str, Any]) -> None:
        await self._ws.send(json.dumps(event))

    async def emit_speech_started(self) -> None:
        await self.send({"type": "input_audio_buffer.speech_started"})

    async def emit_speech_stopped(self) -> None:
        await self.send({"type": "input_audio_buffer.speech_stopped"})

    async def emit_transcript(self, text: str, *, final: bool = True) -> None:
        if final:
            await self.send(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": text,
                }
            )
        else:
            await self.send(
                {"type": "conversation.item.input_audio_transcription.delta", "delta": text}
            )

    async def emit_response_created(
        self, response_id: str, **fields: Any
    ) -> None:
        await self.send(
            {"type": "response.created", "response": {"id": response_id, **fields}}
        )

    async def emit_audio(self, item_id: str, pcm16: bytes) -> None:
        await self.send(
            {
                "type": "response.output_audio.delta",
                "item_id": item_id,
                "delta": base64.b64encode(pcm16).decode("ascii"),
            }
        )

    async def emit_tool_call(self, name: str, arguments: dict[str, Any], call_id: str) -> None:
        await self.send(
            {
                "type": "response.function_call_arguments.done",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments),
            }
        )

    async def emit_response_done(
        self, response_id: str, *, total_tokens: int = 0, **fields: Any
    ) -> None:
        response: dict[str, Any] = {"id": response_id, "status": "completed", **fields}
        if total_tokens:
            response["usage"] = {"total_tokens": total_tokens}
        await self.send({"type": "response.done", "response": response})

    async def emit_active_response_error(self) -> None:
        """The API's refusal of a second concurrent response."""
        await self.send(
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "code": "conversation_already_has_active_response",
                    "message": "Conversation already has an active response",
                },
            }
        )

    async def emit_item_created(self, item_id: str, role: str = "assistant") -> None:
        await self.send(
            {
                "type": "conversation.item.created",
                "item": {"id": item_id, "role": role, "type": "message"},
            }
        )

    # ---------------------------------------------------------- inbound

    async def handle(self, event: dict[str, Any]) -> None:
        self.received.append(event)
        etype = event.get("type", "")
        if etype == "session.update":
            self.session = event.get("session")
            self.session_updated_at = time.monotonic()
            self.session_updated_seq = next(_SEQ)
            await self.send({"type": "session.updated", "session": self.session})
        elif etype == "input_audio_buffer.append":
            self.audio_chunks.append(base64.b64decode(event["audio"]))
        elif etype == "conversation.item.create":
            self._items += 1
            item = dict(event.get("item", {}))
            item.setdefault("id", f"item_{self._items}")
            await self.send({"type": "conversation.item.created", "item": item})
        elif etype == "conversation.item.delete":
            self.deleted_items.append(event["item_id"])
            await self.send(
                {"type": "conversation.item.deleted", "item_id": event["item_id"]}
            )
        elif etype == "conversation.item.truncate":
            self.truncations.append(event)
            await self.send({"type": "conversation.item.truncated", **event})
        elif etype == "response.cancel":
            self.canceled.append(event)
            response_id = event.get("response_id", "")
            if self.active_response_id == response_id:
                self.active_response_id = None
            await self.send(
                {
                    "type": "response.done",
                    "response": {"id": response_id, "status": "cancelled"},
                }
            )
        elif etype == "response.create":
            await self._on_response_create(event.get("response", {}))

    async def _on_response_create(self, request: dict[str, Any]) -> None:
        out_of_band = request.get("conversation") == "none"
        if out_of_band and self.interleave_model_response:
            # The race the provider must survive: the model starts a response
            # of its own between our `response.create` and its `response.created`.
            self.interleave_model_response = False
            await self.start_model_response()
        if self.active_response_id is not None and not out_of_band:
            # What the real API does: a second concurrent response is
            # refused, it is not silently queued.
            self.rejected_responses += 1
            await self.emit_active_response_error()
            return
        self._responses += 1
        response_id = f"resp_{self.index}_{self._responses}"
        # The API echoes back the fields that identify a response, which is
        # how the provider attributes an out-of-band answer.
        echo = {
            key: request[key]
            for key in ("conversation", "output_modalities", "metadata")
            if key in request
        }
        if not out_of_band:
            self.active_response_id = response_id
        await self.emit_response_created(response_id, **echo)
        if out_of_band:
            await self.send(
                {
                    "type": "response.output_text.done",
                    "response_id": response_id,
                    "text": self.summary_text,
                }
            )
            await self.emit_response_done(response_id, **echo)
        elif self.auto_respond:
            await self.finish_response(response_id)

    async def start_model_response(self, *, item_id: str | None = None) -> str:
        """The model answers on its own, as it does after every user turn."""
        self._responses += 1
        response_id = f"resp_{self.index}_{self._responses}"
        self.active_response_id = response_id
        await self.emit_response_created(response_id)
        if item_id is not None:
            await self.emit_item_created(item_id)
        return response_id

    async def finish_response(self, response_id: str, *, total_tokens: int = 0) -> None:
        if self.active_response_id == response_id:
            self.active_response_id = None
        await self.emit_response_done(response_id, total_tokens=total_tokens)

    # --------------------------------------------------------- helpers

    def sent_types(self) -> list[str]:
        return [event.get("type", "") for event in self.received]

    def count(self, etype: str) -> int:
        return self.sent_types().count(etype)

    async def wait_for(self, etype: str, *, count: int = 1, timeout: float = 2.0) -> None:
        """Wait until ``count`` events of type ``etype`` have been received."""
        deadline = time.monotonic() + timeout
        while self.count(etype) < count:
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"timed out waiting for {count}x {etype}; got {self.sent_types()}"
                )
            await asyncio.sleep(0.01)


class FakeRealtimeServer:
    """`websockets` server that hands out a `FakeConnection` per client."""

    def __init__(self) -> None:
        self.connections: list[FakeConnection] = []
        self.url = ""
        self._server: Any = None
        self._new_connection = asyncio.Event()

    async def start(self) -> str:
        self._server = await _ws_serve(self._handler, "127.0.0.1", 0)
        port = next(iter(self._server.sockets)).getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/v1/realtime"
        return self.url

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def connection(self, index: int = 0, *, timeout: float = 2.0) -> FakeConnection:
        """Wait for the connection number ``index`` and return it."""
        deadline = time.monotonic() + timeout
        while len(self.connections) <= index:
            if time.monotonic() >= deadline:
                raise AssertionError(f"no connection #{index} after {timeout}s")
            await asyncio.sleep(0.01)
        connection = self.connections[index]
        await connection.wait_for("session.update", timeout=timeout)
        return connection

    async def _handler(self, ws: Any) -> None:
        connection = FakeConnection(ws, len(self.connections))
        self.connections.append(connection)
        self._new_connection.set()
        await connection.send(
            {"type": "session.created", "session": {"id": f"sess_{connection.index}"}}
        )
        try:
            async for raw in ws:
                await connection.handle(json.loads(raw))
        except Exception:  # client closed mid-frame
            pass
        finally:
            connection.closed_at = time.monotonic()
            connection.closed_seq = next(_SEQ)


async def wait_until(predicate, *, timeout: float = 2.0, message: str = "") -> None:
    """Poll ``predicate`` until it is true, or fail the test."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(message or f"condition still false after {timeout}s")
        await asyncio.sleep(0.01)
