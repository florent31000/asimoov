"""The VAD hysteresis, driven by an injected probability function."""

from __future__ import annotations

import numpy as np
import pytest
from helpers import RecordingBus

from asimoov.contracts.perception import PerceptionContext
from asimoov.contracts.percepts import Bearing
from asimoov.perception import PerceptionError
from asimoov.perception.vad_module import (
    SPEECH_ENDED_TOPIC,
    SPEECH_STARTED_TOPIC,
    WINDOW_SAMPLES,
    VadModule,
)

SILENCE = (b"\x00\x00" * WINDOW_SAMPLES)


class ScriptedProbability:
    def __init__(self, values: list[float]) -> None:
        self.values = values
        self.calls = 0

    def __call__(self, window: np.ndarray) -> float:
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return value


async def started(module: VadModule, bus: RecordingBus) -> VadModule:
    await module.start(PerceptionContext(publish=bus.publish, bus=bus))
    return module


async def test_three_loud_windows_start_speech():
    bus = RecordingBus("perception.vad")
    module = await started(VadModule(prob_fn=ScriptedProbability([0.9])), bus)
    await module.feed(SILENCE * 2)
    assert bus.topics() == []
    await module.feed(SILENCE)
    assert bus.topics() == [SPEECH_STARTED_TOPIC]
    assert module.speaking is True


async def test_speech_ends_after_the_long_tail():
    probabilities = ScriptedProbability([0.9] * 3 + [0.0] * 20)
    bus = RecordingBus("perception.vad")
    module = await started(VadModule(prob_fn=probabilities), bus)
    await module.feed(SILENCE * 3)
    await module.feed(SILENCE * 14)
    assert bus.topics() == [SPEECH_STARTED_TOPIC]
    await module.feed(SILENCE)
    assert bus.topics() == [SPEECH_STARTED_TOPIC, SPEECH_ENDED_TOPIC]
    assert module.speaking is False


async def test_a_single_loud_window_is_not_speech():
    probabilities = ScriptedProbability([0.9, 0.0, 0.9, 0.0])
    bus = RecordingBus("perception.vad")
    module = await started(VadModule(prob_fn=probabilities), bus)
    await module.feed(SILENCE * 4)
    assert bus.topics() == []


async def test_partial_windows_are_buffered_across_calls():
    probabilities = ScriptedProbability([0.9])
    bus = RecordingBus("perception.vad")
    module = await started(VadModule(prob_fn=probabilities), bus)
    half = b"\x00\x00" * (WINDOW_SAMPLES // 2)
    for _ in range(6):
        await module.feed(half)
    assert probabilities.calls == 3
    assert bus.topics() == [SPEECH_STARTED_TOPIC]


async def test_percepts_carry_the_source_and_a_null_direction():
    bus = RecordingBus("perception.vad")
    module = await started(VadModule(prob_fn=ScriptedProbability([0.9])), bus)
    await module.feed(SILENCE * 3)
    data = bus.envelopes[0].data
    assert data["type"] == "speech_started"
    assert data["source"] == "vad_module"
    assert data["direction"] is None


async def test_a_direction_estimator_fills_the_bearing():
    class FakeDoa:
        def estimate(self):
            return Bearing(az=25.0)

    bus = RecordingBus("perception.vad")
    module = await started(VadModule(prob_fn=ScriptedProbability([0.9]), direction=FakeDoa()), bus)
    await module.feed(SILENCE * 3)
    assert bus.envelopes[0].data["direction"] == {"az": 25.0, "el": 0.0}


async def test_percepts_validate_against_the_schema(percept_validator):
    probabilities = ScriptedProbability([0.9] * 3 + [0.0] * 20)
    bus = RecordingBus("perception.vad")
    module = await started(VadModule(prob_fn=probabilities), bus)
    await module.feed(SILENCE * 20)
    assert len(bus.envelopes) == 2
    for envelope in bus.envelopes:
        percept_validator.validate(envelope.data)


async def test_feeding_before_start_is_an_explicit_error():
    with pytest.raises(PerceptionError, match="before start"):
        await VadModule(prob_fn=ScriptedProbability([0.9])).feed(SILENCE)


async def test_stop_resets_the_state():
    bus = RecordingBus("perception.vad")
    module = await started(VadModule(prob_fn=ScriptedProbability([0.9])), bus)
    await module.feed(SILENCE * 3)
    await module.stop()
    assert module.speaking is False
