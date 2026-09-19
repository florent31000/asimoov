"""FakeBody must pass the shared conformance suite, like every real adapter."""

from __future__ import annotations

import pytest

from asimoov.bodies.conformance import run_conformance
from asimoov.contracts.body import BodyContext, BodyManifest
from asimoov.contracts.fakes import FakeAudioSink, FakeAudioSource, FakeBody


@pytest.mark.asyncio
async def test_fake_body_passes_conformance() -> None:
    await run_conformance(FakeBody())


@pytest.mark.asyncio
async def test_fake_body_gesture_times_out_when_too_slow() -> None:
    body = FakeBody(latency_s=1.0)

    await body.start(BodyContext())
    result = await body.gesture("wave_hello", {}, timeout_s=0.05)
    assert result.status.value == "timeout"


def test_fake_body_audio_accessors_follow_manifest_capabilities() -> None:
    default_body = FakeBody()  # manifest declares audio.in and audio.out
    assert isinstance(default_body.audio_source(), FakeAudioSource)
    assert isinstance(default_body.audio_sink(), FakeAudioSink)

    silent_manifest = BodyManifest(name="mute", kind_of_body="virtual", capabilities=())
    silent_body = FakeBody(manifest=silent_manifest)
    assert silent_body.audio_source() is None
    assert silent_body.audio_sink() is None


@pytest.mark.asyncio
async def test_fake_body_simulate_disconnect_triggers_stop_all() -> None:
    body = FakeBody()
    await body.start(BodyContext())

    await body.simulate_disconnect()

    assert body.last_stop_all_reason == "simulated_disconnect"


@pytest.mark.asyncio
async def test_run_conformance_checks_simulate_disconnect_timing() -> None:
    """A body whose simulate_disconnect never calls stop_all must fail conformance."""

    class BrokenDisconnectBody(FakeBody):
        async def simulate_disconnect(self) -> None:
            return None  # never calls stop_all

    with pytest.raises(AssertionError, match="simulate_disconnect"):
        await run_conformance(BrokenDisconnectBody())


@pytest.mark.asyncio
async def test_run_conformance_requires_stop_on_disconnect_declared() -> None:
    """simulate_disconnect() without safety.stop_on_disconnect must fail conformance."""

    manifest = BodyManifest(
        name="undeclared",
        kind_of_body="virtual",
        capabilities=(),
        safety={"watchdog_ms": 500, "stop_on_disconnect": False},
    )
    body = FakeBody(manifest=manifest)

    with pytest.raises(AssertionError, match="stop_on_disconnect"):
        await run_conformance(body)


@pytest.mark.asyncio
async def test_run_conformance_skips_simulate_disconnect_when_absent() -> None:
    """A body with no simulate_disconnect attribute at all must still pass conformance."""

    class NoDisconnectBody(FakeBody):
        simulate_disconnect = None

    await run_conformance(NoDisconnectBody())
