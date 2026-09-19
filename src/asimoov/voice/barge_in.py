"""Barge-in: interrupt the robot while it is speaking, without killing actions.

Sequence (plan.md section 4.4): local VAD on the mic while the tracker reports
playback -> `PlaybackTracker.flush` -> ``response.cancel`` **with** the
``response_id`` (skipped while a local function call is executing, Neon's bug
6) -> ``conversation.item.truncate`` at the real `played_ms` -> mic ungated.

Without a usable local VAD the robot stays half-duplex; `capabilities` reports
which mode is active so ``asimoov doctor`` can say it out loud.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from asimoov.contracts.audio import PlaybackTracker
from asimoov.voice import vad_silero
from asimoov.voice.audio.tracker import DEFAULT_TAIL_MS, MicGate

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BargeInConfig:
    """Tuning of the local detector. Defaults are the plan's values."""

    enabled: bool = True
    k: float = 3.0
    min_speech_ms: float = 300.0
    silero_threshold: float = 0.5
    tail_ms: float = DEFAULT_TAIL_MS


class LocalVad(Protocol):
    """A local speech detector fed with PCM16 mono chunks."""

    name: str

    def feed(self, pcm16: bytes) -> bool:
        """True once speech has lasted long enough to count as a barge-in."""

    def reset(self) -> None:
        """Forget the current speech run (after a detection, or when idle)."""


class RelativeEnergyVad:
    """Fallback detector: RMS above ``k`` times the running noise floor.

    The floor only tracks non-speech chunks, so a steady room tone or a
    constant echo residue raises the bar instead of triggering.
    """

    name = "energy"

    def __init__(
        self,
        *,
        sample_rate_hz: int = 24000,
        k: float = 3.0,
        min_speech_ms: float = 300.0,
        floor_alpha: float = 0.05,
        min_floor: float = 60.0,
    ) -> None:
        self._sample_rate_hz = sample_rate_hz
        self._k = k
        self._min_speech_ms = min_speech_ms
        self._floor_alpha = floor_alpha
        self._min_floor = min_floor
        self._floor: float | None = None
        self._speech_ms = 0.0

    def reset(self) -> None:
        self._speech_ms = 0.0

    def feed(self, pcm16: bytes) -> bool:
        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32)
        if samples.size == 0:
            return False
        rms = float(np.sqrt(np.mean(samples * samples)))
        chunk_ms = samples.size * 1000.0 / self._sample_rate_hz
        if self._floor is None:
            self._floor = max(rms, self._min_floor)
            return False
        floor = max(self._floor, self._min_floor)
        if rms > self._k * floor:
            self._speech_ms += chunk_ms
            if self._speech_ms >= self._min_speech_ms:
                self._speech_ms = 0.0
                return True
            return False
        self._speech_ms = 0.0
        self._floor = (1.0 - self._floor_alpha) * self._floor + self._floor_alpha * rms
        return False


class BargeInDetector:
    """Picks Silero when it is installed, relative energy otherwise."""

    def __init__(
        self,
        *,
        config: BargeInConfig | None = None,
        sample_rate_hz: int = 24000,
        vad: LocalVad | None = None,
    ) -> None:
        self.config = config or BargeInConfig()
        self._sample_rate_hz = sample_rate_hz
        self._reason: str | None = None
        if vad is not None:
            self._vad: LocalVad | None = vad
        elif not self.config.enabled:
            self._vad = None
            self._reason = "disabled by configuration"
        elif vad_silero.available():
            self._vad = vad_silero.SileroVad(
                threshold=self.config.silero_threshold,
                min_speech_ms=self.config.min_speech_ms,
                sample_rate_hz=sample_rate_hz,
            )
        else:
            self._reason = "silero model or onnxruntime missing"
            self._vad = RelativeEnergyVad(
                sample_rate_hz=sample_rate_hz,
                k=self.config.k,
                min_speech_ms=self.config.min_speech_ms,
            )

    @property
    def vad_name(self) -> str:
        return self._vad.name if self._vad is not None else "none"

    def capabilities(self) -> dict[str, Any]:
        """What ``asimoov doctor`` prints about interruptibility."""
        return {
            "mode": "full_duplex" if self._vad is not None else "half_duplex",
            "vad": self.vad_name,
            "min_speech_ms": self.config.min_speech_ms,
            "tail_ms": self.config.tail_ms,
            "reason": self._reason,
        }

    def feed(self, pcm16: bytes) -> bool:
        if self._vad is None:
            return False
        return self._vad.feed(pcm16)

    def reset(self) -> None:
        if self._vad is not None:
            self._vad.reset()


class InterruptibleProvider(Protocol):
    """The slice of `openai_realtime.OpenAIRealtimeProvider` barge-in needs."""

    @property
    def active_response_id(self) -> str | None: ...

    @property
    def current_item_id(self) -> str | None: ...

    @property
    def has_pending_tool_call(self) -> bool: ...

    async def cancel_response(self, response_id: str) -> None: ...

    async def truncate_item(self, item_id: str, played_ms: float) -> None: ...


class BargeInController:
    """Runs the barge-in sequence when the local VAD fires during playback."""

    def __init__(
        self,
        provider: InterruptibleProvider,
        tracker: PlaybackTracker,
        *,
        detector: BargeInDetector | None = None,
        gate: MicGate | None = None,
        on_barge_in: Callable[[str | None], None] | None = None,
    ) -> None:
        self._provider = provider
        self._tracker = tracker
        self._detector = detector or BargeInDetector()
        self._gate = gate
        self._on_barge_in = on_barge_in

    @property
    def detector(self) -> BargeInDetector:
        return self._detector

    def set_provider(self, provider: InterruptibleProvider) -> None:
        """Point the controller at the provider that is live now (renewal)."""
        self._provider = provider

    def capabilities(self) -> dict[str, Any]:
        return self._detector.capabilities()

    async def feed(self, pcm16: bytes) -> bool:
        """Feed a mic chunk captured during playback; True if a barge-in fired."""
        if not self._tracker.is_playing():
            self._detector.reset()
            return False
        if not self._detector.feed(pcm16):
            return False
        return await self.trigger()

    async def trigger(self) -> bool:
        """Interrupt the current response. Returns True once the sequence ran."""
        item_id = self._provider.current_item_id
        response_id = self._provider.active_response_id
        played_ms = self._tracker.played_ms(item_id) if item_id else 0.0

        self._tracker.flush()
        if response_id and not self._provider.has_pending_tool_call:
            await self._provider.cancel_response(response_id)
        elif response_id:
            log.info("barge-in: keeping response %s, a tool call is running", response_id)
        if item_id:
            await self._provider.truncate_item(item_id, played_ms)
        if self._gate is not None:
            self._gate.ungate()
        self._detector.reset()
        if self._on_barge_in is not None:
            self._on_barge_in(item_id)
        return True
