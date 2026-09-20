"""Local turn detection: when does the user's utterance start, and end.

The Realtime API decides this server-side; a text model has nothing of the
kind, so the Claude pipeline decides locally. A speech run of
``min_speech_ms`` opens the turn and ``end_silence_ms`` of silence closes it.

Neither detector is new: `vad_silero.SileroVad` when the ``[vad]`` extra and
the model file are both there, `barge_in.RelativeEnergyVad` otherwise. Both
are built with ``min_speech_ms=0`` so each call answers "is this chunk
speech", and the durations that make a turn are accumulated here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from asimoov.voice import vad_silero
from asimoov.voice.barge_in import LocalVad, RelativeEnergyVad

log = logging.getLogger(__name__)

SPEECH_STARTED = "started"
SPEECH_ENDED = "ended"
DEFAULT_MIN_SPEECH_MS = 300.0
DEFAULT_END_SILENCE_MS = 700.0


@dataclass(frozen=True)
class TurnConfig:
    """Tuning of the local turn detector."""

    min_speech_ms: float = DEFAULT_MIN_SPEECH_MS
    end_silence_ms: float = DEFAULT_END_SILENCE_MS
    silero_threshold: float = 0.5
    k: float = 3.0


class TurnDetector:
    """Answers `SPEECH_STARTED` / `SPEECH_ENDED` / None for each mic chunk."""

    def __init__(
        self,
        *,
        config: TurnConfig | None = None,
        sample_rate_hz: int = 24000,
        vad: LocalVad | None = None,
    ) -> None:
        self.config = config or TurnConfig()
        self._sample_rate_hz = sample_rate_hz
        self._reason: str | None = None
        if vad is not None:
            self._vad = vad
        elif vad_silero.available():
            self._vad = vad_silero.SileroVad(
                threshold=self.config.silero_threshold,
                min_speech_ms=0.0,
                sample_rate_hz=sample_rate_hz,
            )
        else:
            self._reason = "silero model or onnxruntime missing"
            self._vad = RelativeEnergyVad(
                sample_rate_hz=sample_rate_hz, k=self.config.k, min_speech_ms=0.0
            )
        self._speaking = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0

    @property
    def vad_name(self) -> str:
        return self._vad.name

    @property
    def speaking(self) -> bool:
        return self._speaking

    def capabilities(self) -> dict[str, object]:
        """What ``asimoov doctor`` prints about turn taking."""
        return {
            "vad": self.vad_name,
            "min_speech_ms": self.config.min_speech_ms,
            "end_silence_ms": self.config.end_silence_ms,
            "reason": self._reason,
        }

    def reset(self) -> None:
        self._speaking = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._vad.reset()

    def feed(self, pcm16: bytes) -> str | None:
        """Feed one mic chunk; returns a transition or None."""
        if not pcm16:
            return None
        chunk_ms = len(pcm16) / 2 * 1000.0 / self._sample_rate_hz
        speech = self._vad.feed(pcm16)
        if not self._speaking:
            if not speech:
                self._speech_ms = 0.0
                return None
            self._speech_ms += chunk_ms
            if self._speech_ms < self.config.min_speech_ms:
                return None
            self._speaking = True
            self._silence_ms = 0.0
            return SPEECH_STARTED
        if speech:
            self._silence_ms = 0.0
            return None
        self._silence_ms += chunk_ms
        if self._silence_ms < self.config.end_silence_ms:
            return None
        self._speaking = False
        self._speech_ms = 0.0
        return SPEECH_ENDED


__all__ = [
    "DEFAULT_END_SILENCE_MS",
    "DEFAULT_MIN_SPEECH_MS",
    "SPEECH_ENDED",
    "SPEECH_STARTED",
    "TurnConfig",
    "TurnDetector",
]
