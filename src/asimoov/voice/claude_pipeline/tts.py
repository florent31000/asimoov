"""Text to speech for the Claude pipeline.

One protocol, one real engine (Kokoro through `kokoro-onnx`, extra
``[claude]``) and one fake. Kokoro runs at 24 kHz natively, the rate the rest
of the pipeline uses, and ships exactly one French voice (``ff_siwis``) --
see `docs/voice.md`. A cloud engine (ElevenLabs) plugs in by implementing
`TextToSpeech` and adding a name to `build_tts`; none is implemented here.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from asimoov.core.config import asimoov_home
from asimoov.voice.audio.resample import resample_pcm16

log = logging.getLogger(__name__)

DEFAULT_ENGINE = "kokoro"
KOKORO_RATE_HZ = 24000
MODEL_ENV_VAR = "ASIMOOV_KOKORO_MODEL"
VOICES_ENV_VAR = "ASIMOOV_KOKORO_VOICES"
MODEL_FILENAME = "kokoro-v1.0.onnx"
VOICES_FILENAME = "voices-v1.0.bin"

#: Kokoro's espeak language code and default voice, per persona language.
#: French has a single voice in Kokoro-82M (``ff_siwis``); any other language
#: must name its voice in the persona rather than get a guessed one.
KOKORO_DEFAULTS: dict[str, tuple[str, str]] = {
    "en": ("en-us", "af_heart"),
    "fr": ("fr-fr", "ff_siwis"),
}


class TextToSpeech(Protocol):
    """Synthesizes one sentence of PCM16 mono audio."""

    name: str

    async def synthesize(self, text: str, *, sample_rate_hz: int) -> bytes:
        """Return PCM16 mono at ``sample_rate_hz`` (empty for empty text)."""

    def available(self) -> tuple[bool, str]:
        """``(usable, explanation)`` for ``asimoov doctor``."""


def _kokoro_onnx() -> Any:
    try:
        import kokoro_onnx
    except ImportError:  # pragma: no cover - depends on the [claude] extra
        return None
    return kokoro_onnx


def kokoro_model_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    from_env = os.environ.get(MODEL_ENV_VAR)
    return Path(from_env) if from_env else asimoov_home() / "models" / MODEL_FILENAME


def kokoro_voices_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    from_env = os.environ.get(VOICES_ENV_VAR)
    return Path(from_env) if from_env else asimoov_home() / "models" / VOICES_FILENAME


class KokoroTTS:
    """Kokoro-82M through ONNX Runtime, loaded on first use.

    Needs `espeak-ng` installed system-wide (`kokoro-onnx` phonemizes with
    it) plus the two model files under ``$ASIMOOV_HOME/models``.
    """

    name = "kokoro"

    def __init__(
        self,
        *,
        voice: str,
        lang: str,
        speed: float = 1.0,
        model_path: str | os.PathLike[str] | None = None,
        voices_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.voice = voice
        self.lang = lang
        self.speed = speed
        self.model_path = kokoro_model_path(model_path)
        self.voices_path = kokoro_voices_path(voices_path)
        self._kokoro: Any = None
        self._lock = asyncio.Lock()

    def available(self) -> tuple[bool, str]:
        if _kokoro_onnx() is None:
            return False, "kokoro-onnx is not installed (pip install 'asimoov[claude]')"
        missing = [str(p) for p in (self.model_path, self.voices_path) if not p.is_file()]
        if missing:
            return False, "missing model file(s): " + ", ".join(missing)
        return True, f"voice {self.voice} ({self.lang})"

    async def warm_up(self) -> None:
        await self._ensure_engine()

    async def _ensure_engine(self) -> Any:
        if self._kokoro is not None:
            return self._kokoro
        async with self._lock:
            if self._kokoro is None:
                usable, reason = self.available()
                if not usable:
                    raise RuntimeError(f"kokoro TTS unavailable: {reason}")
                module = _kokoro_onnx()
                self._kokoro = await asyncio.to_thread(
                    module.Kokoro, str(self.model_path), str(self.voices_path)
                )
        return self._kokoro

    async def synthesize(self, text: str, *, sample_rate_hz: int) -> bytes:
        if not text.strip():
            return b""
        engine = await self._ensure_engine()
        samples, rate = await asyncio.to_thread(
            engine.create, text, voice=self.voice, speed=self.speed, lang=self.lang
        )
        pcm16 = (
            np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767.0
        ).astype(np.int16).tobytes()
        return resample_pcm16(pcm16, int(rate), sample_rate_hz)


class FakeTTS:
    """A 220 Hz tone, 60 ms per character: audible, deterministic, silent-free."""

    name = "fake"

    def __init__(self, *, ms_per_char: float = 60.0, frequency_hz: float = 220.0) -> None:
        self.ms_per_char = ms_per_char
        self.frequency_hz = frequency_hz
        self.spoken: list[str] = []

    def available(self) -> tuple[bool, str]:
        return True, "synthesized tone"

    async def warm_up(self) -> None:
        return None

    async def synthesize(self, text: str, *, sample_rate_hz: int) -> bytes:
        if not text.strip():
            return b""
        self.spoken.append(text)
        count = max(1, int(sample_rate_hz * len(text) * self.ms_per_char / 1000.0))
        t = np.arange(count, dtype=np.float32) / sample_rate_hz
        wave = np.sin(2.0 * math.pi * self.frequency_hz * t) * 0.2
        return (wave * 32767.0).astype(np.int16).tobytes()


#: Built engines, keyed by their settings; see `stt.build_stt` for why a
#: renewal must not load a second copy of the same ONNX session.
_BUILT: dict[tuple[Any, ...], TextToSpeech] = {}


def build_tts(config: dict[str, Any], *, language: str | None = None) -> TextToSpeech:
    """Build (or reuse) the engine named by ``config['engine']`` (default Kokoro).

    Raises:
        ValueError: on an unknown engine, or on a language Kokoro has no
            documented default voice for and the persona did not name one.
    """
    engine = str(config.get("engine", DEFAULT_ENGINE))
    if engine == "fake":
        return FakeTTS()
    if engine != "kokoro":
        raise ValueError(f"unknown tts engine {engine!r}, expected 'kokoro' or 'fake'")
    code = (language or "en").split("-")[0].lower()
    default_lang, default_voice = KOKORO_DEFAULTS.get(code, (code, ""))
    voice = str(config.get("voice") or default_voice)
    if not voice:
        raise ValueError(
            f"no default Kokoro voice for language {code!r}: set persona voice.options.tts.voice"
        )
    key = (
        "kokoro",
        voice,
        str(config.get("lang") or default_lang),
        float(config.get("speed", 1.0)),
        str(config.get("model_path") or ""),
        str(config.get("voices_path") or ""),
    )
    if key not in _BUILT:
        _BUILT[key] = KokoroTTS(
            voice=key[1],
            lang=key[2],
            speed=key[3],
            model_path=config.get("model_path"),
            voices_path=config.get("voices_path"),
        )
    return _BUILT[key]


__all__ = [
    "KOKORO_DEFAULTS",
    "DEFAULT_ENGINE",
    "FakeTTS",
    "KokoroTTS",
    "TextToSpeech",
    "build_tts",
    "kokoro_model_path",
    "kokoro_voices_path",
]
