"""Voice provider contract: the ABC behind speech-to-speech conversation.

V1 has one implementation (OpenAI Realtime, WS2); V1.1 adds a
STT->LLM->TTS pipeline for other providers behind the same ABC. See plan.md
section 4.4 and the Neon bugs this contract exists to prevent (section 2,
items 1, 2, 5, 6, 7).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol

from asimoov.contracts.percepts import Utterance


class VoiceEvents(Protocol):
    """Callbacks a `VoiceProvider` invokes as conversation events happen.

    Implemented by the core (WS1); called from whatever thread/task the
    provider uses internally, so implementations must be safe to call from
    off the main event loop (e.g. via ``call_soon_threadsafe``) if the
    provider itself runs a receive loop on a different task.
    """

    def on_speech_started(self) -> None:
        """User speech detected (server VAD). Used to gate mic/UI state."""

    def on_speech_ended(self) -> None:
        """User speech ended (server VAD)."""

    def on_utterance(self, utterance: Utterance) -> None:
        """A transcript chunk is available (may be partial or final)."""

    def on_tool_call(self, name: str, params: dict[str, Any], call_id: str) -> None:
        """The model invoked a tool. The core must eventually reply via the

        provider-specific function-call-output mechanism; see
        `request_response`.
        """

    def on_audio_out(self, item_id: str, pcm16: bytes) -> None:
        """A chunk of synthesized speech audio is ready to play.

        ``pcm16`` follows the PCM format documented in `contracts.audio`
        (signed 16-bit little-endian, mono) at the provider's
        `VoiceProvider.sample_rate_hz`; the core resamples if the
        `AudioSink` runs at another rate.
        """

    def on_response_done(self, response_id: str) -> None:
        """A response finished (naturally or via `cancel_response`)."""


class VoiceProvider(ABC):
    """A speech-to-speech (or STT->LLM->TTS) conversation backend.

    Attributes:
        sample_rate_hz: Rate of the PCM16 mono audio exchanged with this
            provider, both ways (`send_audio` and `VoiceEvents.on_audio_out`).
            Defaults to 24000, the OpenAI Realtime PCM16 rate; a provider
            that negotiates another rate overrides it before `start`.
    """

    sample_rate_hz: int = 24000

    @abstractmethod
    async def start(self, events: VoiceEvents, config: dict[str, Any]) -> None:
        """Connect and begin a session.

        Must not block for more than 100 ms; the actual connection runs in
        an internal task with its own retry/backoff, matching `Body.start`.
        ``config`` carries the persona's ``voice`` section (provider, model,
        voice, transcription_language).
        """

    @abstractmethod
    async def stop(self) -> None:
        """End the session and release resources. Idempotent."""

    @abstractmethod
    async def send_audio(self, pcm16: bytes) -> None:
        """Forward one chunk of microphone audio to the provider.

        ``pcm16`` follows the PCM format documented in `contracts.audio`
        (signed 16-bit little-endian, mono) at ``sample_rate_hz``; the
        caller resamples beforehand if its `AudioSource` runs at another
        rate. Callers are responsible for mic gating (never call this while the
        `PlaybackTracker` reports `is_playing()`, per plan.md section 4.4);
        this method does not gate on its own.
        """

    @abstractmethod
    async def inject_system_text(self, text: str) -> None:
        """Inject a short system-role message (a mind/perception update).

        Must never be called while a response is actively streaming; the
        core queues injections and flushes them at ``response.done``
        (coalesced, budgeted, deduplicated -- see plan.md section 4.4).
        Implementations do not enforce this policy themselves.
        """

    @abstractmethod
    async def request_response(self, instructions: str | None = None) -> None:
        """Ask the provider to produce a response now.

        Used both for normal turn-taking follow-ups (e.g. after a tool
        result) and for initiative. Must only be called when no response is
        currently active; calling it during an active response is a caller
        bug, not something this method silently corrects.
        """

    @abstractmethod
    async def cancel_response(self, response_id: str) -> None:
        """Cancel an in-flight response, identified by ``response_id``.

        ``response_id`` is required and must refer to the response actually
        being canceled: Neon's bug (plan.md section 2, item 6) was calling
        the equivalent of this without an id and killing unrelated actions.
        No keyword-matching fallback is acceptable here.
        """

    @abstractmethod
    async def truncate_item(self, item_id: str, played_ms: float) -> None:
        """Truncate conversation item ``item_id`` at ``played_ms``.

        Called on barge-in so the model's context matches what the user
        actually heard, using `PlaybackTracker.played_ms(item_id)` as the
        source of truth for ``played_ms``.
        """
