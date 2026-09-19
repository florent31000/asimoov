"""Silero VAD (ONNX) wrapper for local barge-in detection.

`onnxruntime` is an optional dependency (extra ``[vad]``) and the model file is
downloaded, not vendored: `available` answers whether this detector can run at
all, and `barge_in.BargeInDetector` falls back to relative energy when it
cannot.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

from asimoov.core.config import asimoov_home
from asimoov.voice.audio.resample import resample_pcm16


def _onnxruntime():
    """Import `onnxruntime` on demand, or None if the [vad] extra is absent.

    Deferred on purpose: `onnxruntime` loads ~100 MB of native libraries,
    and importing `asimoov.voice` (which the core does to reach the session
    manager and the audio tracker) must not pay for a detector nobody has
    asked for yet.
    """
    try:
        import onnxruntime
    except ImportError:  # pragma: no cover - depends on the [vad] extra
        return None
    return onnxruntime

log = logging.getLogger(__name__)

MODEL_ENV_VAR = "ASIMOOV_SILERO_MODEL"
MODEL_FILENAME = "silero_vad.onnx"
SILERO_RATE_HZ = 16000
FRAME_SAMPLES = 512
FRAME_MS = FRAME_SAMPLES * 1000 / SILERO_RATE_HZ


def model_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Where the Silero model lives: explicit, ``$ASIMOOV_SILERO_MODEL``, home.

    The home directory is `core.config.asimoov_home`, not a second copy of
    the same rule: ``$ASIMOOV_HOME`` moves this file too (review, medium).
    """
    if explicit is not None:
        return Path(explicit)
    from_env = os.environ.get(MODEL_ENV_VAR)
    if from_env:
        return Path(from_env)
    return asimoov_home() / "models" / MODEL_FILENAME


def available(explicit: str | os.PathLike[str] | None = None) -> bool:
    """True when `onnxruntime` and the Silero model file are both present."""
    return model_path(explicit).is_file() and _onnxruntime() is not None


class SileroVad:
    """Speech probability per 32 ms frame, latched over ``min_speech_ms``.

    Args:
        path: Silero ONNX model; defaults to ``$ASIMOOV_SILERO_MODEL`` then
            ``$ASIMOOV_HOME/models/silero_vad.onnx``.
        sample_rate_hz: Rate of the PCM16 fed to `feed` (resampled to 16 kHz).

    Raises:
        RuntimeError: if `onnxruntime` or the model file is missing.
    """

    name = "silero"

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        threshold: float = 0.5,
        min_speech_ms: float = 300.0,
        sample_rate_hz: int = 24000,
    ) -> None:
        onnxruntime = _onnxruntime()
        if onnxruntime is None:
            raise RuntimeError("onnxruntime is not installed: pip install 'asimoov[vad]'")
        resolved = model_path(path)
        if not resolved.is_file():
            raise RuntimeError(f"Silero model not found: {resolved}")
        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(str(resolved), sess_options=options)
        self._input_names = {spec.name for spec in self._session.get_inputs()}
        self._threshold = threshold
        self._min_speech_ms = min_speech_ms
        self._sample_rate_hz = sample_rate_hz
        self._buffer = np.zeros(0, dtype=np.float32)
        self._speech_ms = 0.0
        self._reset_state()

    def _reset_state(self) -> None:
        if "state" in self._input_names:  # Silero v5
            self._state = np.zeros((2, 1, 128), dtype=np.float32)
        else:  # Silero v4
            self._state = None
            self._h = np.zeros((2, 1, 64), dtype=np.float32)
            self._c = np.zeros((2, 1, 64), dtype=np.float32)

    def reset(self) -> None:
        self._buffer = np.zeros(0, dtype=np.float32)
        self._speech_ms = 0.0
        self._reset_state()

    def feed(self, pcm16: bytes) -> bool:
        """Feed PCM16 mono; True once speech has lasted ``min_speech_ms``."""
        if self._sample_rate_hz != SILERO_RATE_HZ:
            pcm16 = resample_pcm16(pcm16, self._sample_rate_hz, SILERO_RATE_HZ)
        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        self._buffer = np.concatenate([self._buffer, samples])
        detected = False
        while self._buffer.size >= FRAME_SAMPLES:
            frame = self._buffer[:FRAME_SAMPLES]
            self._buffer = self._buffer[FRAME_SAMPLES:]
            if self._probability(frame) >= self._threshold:
                self._speech_ms += FRAME_MS
                if self._speech_ms >= self._min_speech_ms:
                    self._speech_ms = 0.0
                    detected = True
            else:
                self._speech_ms = 0.0
        return detected

    def _probability(self, frame: np.ndarray) -> float:
        inputs = {"input": frame.reshape(1, -1), "sr": np.array(SILERO_RATE_HZ, dtype=np.int64)}
        if self._state is not None:
            inputs["state"] = self._state
            output, self._state = self._session.run(None, inputs)
        else:
            inputs["h"] = self._h
            inputs["c"] = self._c
            output, self._h, self._c = self._session.run(None, inputs)
        return float(np.asarray(output).reshape(-1)[0])
