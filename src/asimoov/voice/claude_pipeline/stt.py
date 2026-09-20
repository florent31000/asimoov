"""Speech to text for the Claude pipeline.

One protocol, one real engine (`faster-whisper`, CPU int8, extra
``[claude]``) and one fake. A cloud engine (Deepgram, ElevenLabs Scribe)
plugs in by implementing `SpeechToText` and adding a name to `build_stt`;
none is implemented here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

import numpy as np

log = logging.getLogger(__name__)

DEFAULT_ENGINE = "faster_whisper"
DEFAULT_MODEL = "small"


class SpeechToText(Protocol):
    """Transcribes one complete utterance of PCM16 mono audio."""

    name: str

    async def transcribe(
        self, pcm16: bytes, *, sample_rate_hz: int, language: str | None = None
    ) -> str:
        """Return the transcript, or an empty string when nothing was said."""

    def available(self) -> tuple[bool, str]:
        """``(usable, explanation)`` for ``asimoov doctor``."""


def _faster_whisper() -> Any:
    try:
        import faster_whisper
    except ImportError:  # pragma: no cover - depends on the [claude] extra
        return None
    return faster_whisper


class FasterWhisperSTT:
    """Local Whisper through CTranslate2, loaded on first use.

    The model is a few hundred megabytes and takes seconds to load, so it is
    loaded in a worker thread the first time an utterance arrives (and by
    `warm_up` at startup), never at import time.
    """

    name = "faster_whisper"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        language: str | None = None,
        device: str = "cpu",
        compute_type: str = "int8",
        beam_size: int = 1,
    ) -> None:
        self.model = model
        self.language = language
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size
        self._model: Any = None
        self._lock = asyncio.Lock()

    def available(self) -> tuple[bool, str]:
        if _faster_whisper() is None:
            return False, "faster-whisper is not installed (pip install 'asimoov[claude]')"
        return True, f"model {self.model} on {self.device} ({self.compute_type})"

    async def warm_up(self) -> None:
        """Load the model now, so the first utterance does not pay for it."""
        await self._ensure_model()

    async def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        async with self._lock:
            if self._model is None:
                module = _faster_whisper()
                if module is None:
                    raise RuntimeError(
                        "faster-whisper is not installed (pip install 'asimoov[claude]')"
                    )
                self._model = await asyncio.to_thread(
                    module.WhisperModel,
                    self.model,
                    device=self.device,
                    compute_type=self.compute_type,
                )
        return self._model

    async def transcribe(
        self, pcm16: bytes, *, sample_rate_hz: int, language: str | None = None
    ) -> str:
        if not pcm16:
            return ""
        model = await self._ensure_model()
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        if sample_rate_hz != 16000:
            from asimoov.voice.audio.resample import resample_pcm16

            resampled = resample_pcm16(pcm16, sample_rate_hz, 16000)
            audio = np.frombuffer(resampled, dtype=np.int16).astype(np.float32) / 32768.0
        return await asyncio.to_thread(
            self._transcribe_sync, model, audio, language or self.language
        )

    def _transcribe_sync(self, model: Any, audio: np.ndarray, language: str | None) -> str:
        segments, _info = model.transcribe(audio, language=language, beam_size=self.beam_size)
        return "".join(segment.text for segment in segments).strip()


class FakeSTT:
    """Returns scripted transcripts, one per utterance, then the last one."""

    name = "fake"

    def __init__(self, transcripts: str | list[str] | tuple[str, ...] = "") -> None:
        self.transcripts = [transcripts] if isinstance(transcripts, str) else list(transcripts)
        self.calls: list[bytes] = []

    def available(self) -> tuple[bool, str]:
        return True, "scripted transcripts"

    async def warm_up(self) -> None:
        return None

    async def transcribe(
        self, pcm16: bytes, *, sample_rate_hz: int, language: str | None = None
    ) -> str:
        self.calls.append(pcm16)
        if not self.transcripts:
            return ""
        index = min(len(self.calls) - 1, len(self.transcripts) - 1)
        return self.transcripts[index]


#: Built engines, keyed by their settings. A session renewal starts a second
#: provider while the first is still serving audio; reloading a Whisper model
#: already resident would cost seconds and twice the memory, and transcription
#: carries no state between calls, so the same object serves both.
_BUILT: dict[tuple[Any, ...], SpeechToText] = {}


def build_stt(config: dict[str, Any], *, language: str | None = None) -> SpeechToText:
    """Build (or reuse) the engine named by ``config['engine']``.

    Raises:
        ValueError: on an unknown engine name; a typo must not silently fall
            back to a robot that transcribes nothing.
    """
    engine = str(config.get("engine", DEFAULT_ENGINE))
    if engine == "fake":
        return FakeSTT(config.get("transcripts", ""))
    if engine not in ("faster_whisper", "faster-whisper"):
        raise ValueError(f"unknown stt engine {engine!r}, expected 'faster_whisper' or 'fake'")
    key = (
        "faster_whisper",
        str(config.get("model", DEFAULT_MODEL)),
        config.get("language", language),
        str(config.get("device", "cpu")),
        str(config.get("compute_type", "int8")),
    )
    if key not in _BUILT:
        _BUILT[key] = FasterWhisperSTT(
            model=key[1], language=key[2], device=key[3], compute_type=key[4]
        )
    return _BUILT[key]


__all__ = ["DEFAULT_ENGINE", "DEFAULT_MODEL", "FakeSTT", "FasterWhisperSTT", "SpeechToText", "build_stt"]
