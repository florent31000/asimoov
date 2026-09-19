"""Reusable conformance suite every `Body` adapter must pass (plan.md section 4.5).

Import `run_conformance` from a pytest test in the adapter's own test
directory (``tests/bodies/<name>/test_conformance.py``):

    import pytest
    from asimoov.bodies.conformance import run_conformance
    from my_adapter import MyBody

    @pytest.mark.asyncio
    async def test_conformance():
        await run_conformance(MyBody())

This module intentionally only checks what is testable against the `Body`
ABC alone (manifest validity, `implements` executing, `stop_all` timing and
idempotency, `look_at` at 10 Hz without task leak, start/stop lifecycle
reflected in `health()`). The suite leaves the body stopped.

If a body implements the optional ``async def simulate_disconnect(self) ->
None`` protocol, the suite also checks that it triggers `stop_all` within
1 s and that `BodyManifest.safety["stop_on_disconnect"]` is declared;
bodies that do not implement it skip this check.

Adapter-specific behavior -- a real network disconnect actually triggering
`stop_all`, obstacle avoidance, forbidden-flip filtering -- is verified by
each workstream's own adapter tests, since it depends on hardware/transport
details contracts deliberately does not model.
"""

from __future__ import annotations

import asyncio
import time

from asimoov.contracts.behaviors import BehaviorResult
from asimoov.contracts.body import Body, BodyContext, GazeTarget
from asimoov.contracts.face import FaceState
from asimoov.contracts.vocab import BODY_KINDS, is_capability

STOP_ALL_BUDGET_S = 0.2
LOOK_AT_CALL_BUDGET_S = 0.05
LOOK_AT_SAMPLE_COUNT = 10
LOOK_AT_INTERVAL_S = 0.1  # 10 Hz


async def run_conformance(body: Body, *, ctx: BodyContext | None = None) -> None:
    """Run the full conformance suite against ``body``.

    Raises:
        AssertionError: on the first violated requirement, naming it.
    """
    _check_manifest(body)
    await _check_start_stop_lifecycle(body, ctx or BodyContext())
    await _check_implements_executable(body)
    await _check_look_at_throughput(body)
    await _check_set_face_noop_safe(body)
    await _check_stop_all(body)
    _check_audio_accessors(body)
    await _check_simulate_disconnect(body)
    await body.stop()


def _check_manifest(body: Body) -> None:
    manifest = body.manifest
    assert manifest is not None, "Body.manifest must be set"
    assert manifest.name, "BodyManifest.name must not be empty"
    assert manifest.kind_of_body in BODY_KINDS, f"invalid kind_of_body: {manifest.kind_of_body!r}"
    for capability in manifest.capabilities:
        assert is_capability(capability), (
            f"unknown capability {capability!r}: capabilities come from vocab.CAPABILITIES"
        )
    for behavior_name, spec in manifest.implements.items():
        assert isinstance(spec, dict) and spec.get("primitive"), (
            f"implements[{behavior_name!r}] must be a dict with a non-empty 'primitive'"
        )


async def _check_start_stop_lifecycle(body: Body, ctx: BodyContext) -> None:
    await body.start(ctx)
    health = await body.health()
    assert health.connected, "Body.health().connected must be True after start()"

    await body.stop()
    health = await body.health()
    assert not health.connected, "Body.health().connected must be False after stop()"

    # Restart for the remaining checks.
    await body.start(ctx)


async def _check_implements_executable(body: Body) -> None:
    for behavior_name in body.manifest.implements:
        result = await asyncio.wait_for(
            body.gesture(behavior_name, {}, timeout_s=2.0), timeout=3.0
        )
        assert isinstance(result, BehaviorResult), (
            f"gesture({behavior_name!r}) must return a BehaviorResult, got {type(result)!r}"
        )


async def _check_look_at_throughput(body: Body) -> None:
    """10 Hz for a full second: every call near-instant, and no task leak."""
    tasks_before = len(asyncio.all_tasks())
    for i in range(LOOK_AT_SAMPLE_COUNT):
        target = GazeTarget(az=float(i), el=0.0)
        started = time.monotonic()
        await asyncio.wait_for(body.look_at(target), timeout=LOOK_AT_CALL_BUDGET_S)
        elapsed = time.monotonic() - started
        assert elapsed < LOOK_AT_CALL_BUDGET_S, (
            f"look_at() call {i} took {elapsed * 1000:.1f} ms, must be near-instant (fire-and-forget)"
        )
        await asyncio.sleep(LOOK_AT_INTERVAL_S)

    tasks_after = len(asyncio.all_tasks())
    assert tasks_after <= tasks_before + 1, (
        f"look_at() leaked tasks: {tasks_before} before, {tasks_after} after "
        f"{LOOK_AT_SAMPLE_COUNT} calls at 10 Hz (smooth toward the latest target in one "
        "long-lived task, do not spawn one per call)"
    )


async def _check_set_face_noop_safe(body: Body) -> None:
    # Must not raise even if the body has no face.* capability.
    await body.set_face(FaceState(emotion="neutral"))


async def _check_stop_all(body: Body) -> None:
    started = time.monotonic()
    await body.stop_all("conformance")
    elapsed = time.monotonic() - started
    assert elapsed < STOP_ALL_BUDGET_S, f"stop_all() took {elapsed * 1000:.1f} ms, must be < 200 ms"

    # Idempotent: calling it again must not raise.
    await body.stop_all("conformance-again")


def _check_audio_accessors(body: Body) -> None:
    body.audio_source()
    body.audio_sink()


async def _check_simulate_disconnect(body: Body) -> None:
    """If ``body`` implements the optional ``simulate_disconnect()`` protocol,
    check it triggers ``stop_all`` within 1 s and that the manifest declares
    ``safety.stop_on_disconnect`` (a body that reacts to disconnects must say
    so, so `SafetyGuard` and operators know to expect it).
    """
    simulate = getattr(body, "simulate_disconnect", None)
    if simulate is None:
        return

    assert body.manifest.safety.get("stop_on_disconnect"), (
        "a Body implementing simulate_disconnect() must declare "
        "manifest.safety.stop_on_disconnect = true"
    )

    stop_all_called = asyncio.Event()
    original_stop_all = body.stop_all

    async def _tracking_stop_all(reason: str) -> None:
        await original_stop_all(reason)
        stop_all_called.set()

    body.stop_all = _tracking_stop_all  # type: ignore[method-assign]
    try:
        await simulate()
        try:
            await asyncio.wait_for(stop_all_called.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            raise AssertionError(
                "simulate_disconnect() must trigger stop_all() within 1 s"
            ) from None
    finally:
        body.stop_all = original_stop_all  # type: ignore[method-assign]
