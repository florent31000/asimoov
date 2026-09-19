"""Local voice activity detection (Silero ONNX) -> speech_started / speech_ended.

Turn-taking in production comes from the Realtime server VAD (WS2); this
module exists for replay and bench runs, and for the barge-in path when the
voice provider has no server VAD. It consumes PCM16 pushed by its caller and
publishes percepts; it never opens an audio device itself.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import numpy as np

from asimoov.contracts.perception import PerceptionContext, PerceptionModule
from asimoov.contracts.percepts import SpeechEnded, SpeechStarted
from asimoov.perception import PerceptionError
from asimoov.perception.direction_stub import DirectionEstimator, NullDirectionEstimator
from asimoov.perception.models import SILERO_VAD, require

log = logging.getLogger(__name__)

SPEECH_STARTED_TOPIC = "percept.speech_started"
SPEECH_ENDED_TOPIC = "percept.speech_ended"
SOURCE = "vad_module"

#: Silero v5 expects exactly 512 samples at 16 kHz per call.
SAMPLE_RATE_HZ = 16000
WINDOW_SAMPLES = 512
DEFAULT_START_PROB = 0.5
DEFAULT_END_PROB = 0.35
DEFAULT_START_WINDOWS = 3
DEFAULT_END_WINDOWS = 15

ProbFn = Callable[[np.ndarray], float]


class SileroProbability:
    """Speech probability per 512-sample window, from the Silero ONNX model.

    Raises:
        PerceptionError: if onnxruntime or the model file is missing.
    """

    def __init__(self, *, providers: list[str] | None = None) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - depends on the [vad] extra
            raise PerceptionError("the VAD module needs `pip install asimoov[vad]`") from exc
        self._session = ort.InferenceSession(
            str(require(SILERO_VAD)), providers=providers or ["CPUExecutionProvider"]
        )
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def __call__(self, window: np.ndarray) -> float:
        output, self._state = self._session.run(
            None,
            {
                "input": window.reshape(1, -1).astype(np.float32),
                "state": self._state,
                "sr": np.array(SAMPLE_RATE_HZ, dtype=np.int64),
            },
        )
        return float(output[0][0])


class VadModule(PerceptionModule):
    """Hysteresis over per-window speech probabilities.

    ``prob_fn`` is injected so the state machine is testable without the
    model; left to None it builds `SileroProbability` at `start`.
    """

    def __init__(
        self,
        *,
        prob_fn: ProbFn | None = None,
        direction: DirectionEstimator | None = None,
        start_prob: float = DEFAULT_START_PROB,
        end_prob: float = DEFAULT_END_PROB,
        start_windows: int = DEFAULT_START_WINDOWS,
        end_windows: int = DEFAULT_END_WINDOWS,
    ) -> None:
        self._prob_fn = prob_fn
        self.direction = direction or NullDirectionEstimator()
        self.start_prob = start_prob
        self.end_prob = end_prob
        self.start_windows = start_windows
        self.end_windows = end_windows
        self.speaking = False
        self._above = 0
        self._below = 0
        self._tail = np.zeros(0, dtype=np.float32)
        self._ctx: PerceptionContext | None = None
        self._started = False

    async def start(self, ctx: PerceptionContext) -> None:
        self._ctx = ctx
        if self._prob_fn is None:
            self._prob_fn = SileroProbability()
        self._started = True

    async def stop(self) -> None:
        self._started = False
        self.speaking = False
        self._above = self._below = 0
        self._tail = np.zeros(0, dtype=np.float32)

    async def handle_command(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        raise KeyError(name)

    async def feed(self, pcm16: bytes) -> None:
        """Push mono PCM16 at 16 kHz; publishes percepts on each transition."""
        if not self._started or self._prob_fn is None:
            raise PerceptionError("VadModule.feed called before start()")
        samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        self._tail = np.concatenate([self._tail, samples])
        while self._tail.size >= WINDOW_SAMPLES:
            window, self._tail = self._tail[:WINDOW_SAMPLES], self._tail[WINDOW_SAMPLES:]
            await self._step(self._prob_fn(window))

    async def _step(self, probability: float) -> None:
        if probability >= self.start_prob:
            self._above += 1
            self._below = 0
        elif probability < self.end_prob:
            self._below += 1
            self._above = 0
        if not self.speaking and self._above >= self.start_windows:
            self.speaking = True
            self._above = 0
            await self._publish(SPEECH_STARTED_TOPIC, SpeechStarted(
                source=SOURCE, direction=self.direction.estimate()
            ).to_dict())
        elif self.speaking and self._below >= self.end_windows:
            self.speaking = False
            self._below = 0
            await self._publish(SPEECH_ENDED_TOPIC, SpeechEnded(
                source=SOURCE, direction=self.direction.estimate()
            ).to_dict())

    async def _publish(self, topic: str, data: dict[str, Any]) -> None:
        ctx = self._ctx
        if ctx is None or ctx.publish is None:
            return
        await ctx.publish(topic, data, kind="percept")
