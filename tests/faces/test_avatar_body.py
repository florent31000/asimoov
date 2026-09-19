"""AvatarBody: conformance, face animations, gaze mapping."""

from __future__ import annotations

import asyncio

from asimoov.bodies.avatar import AvatarBody
from asimoov.bodies.conformance import run_conformance
from asimoov.contracts.behaviors import BehaviorStatus
from asimoov.contracts.body import BodyContext, GazeTarget
from asimoov.contracts.face import FaceState


async def test_avatar_passes_the_conformance_suite() -> None:
    await run_conformance(AvatarBody())


def test_manifest_declares_a_screen_face_and_host_audio() -> None:
    body = AvatarBody()
    assert body.manifest.kind_of_body == "virtual"
    assert set(body.manifest.capabilities) >= {"face.screen", "audio.in", "audio.out"}
    assert body.manifest.safety["stop_on_disconnect"] is True
    assert body.audio_source() is None, "the core picks the host default microphone"
    assert body.audio_sink() is None, "the core picks the host default speaker"


async def test_look_at_moves_the_gaze_toward_the_robots_own_left(bus) -> None:
    body = AvatarBody()
    await body.start(BodyContext(bus=bus))
    try:
        await body.look_at(GazeTarget(az=45.0, el=15.0))
        await asyncio.sleep(0.5)
        state = body.current_face()
        assert state.gaze.x > 0.8, "az > 0 (robot's left) must give gaze.x > 0"
        assert state.gaze.y > 0.3, "el > 0 (up) must give gaze.y > 0"
        assert bus.published, "a gaze move must reach the mind as an overlay"
        topic, data, kind = bus.published[-1]
        assert (topic, kind) == ("face.overlay", "state"), "the mind owns face.state"
        assert data["gaze"]["x"] > 0.8
    finally:
        await body.stop()


async def test_gaze_is_clamped_to_the_manifest_limits() -> None:
    body = AvatarBody()
    await body.start(BodyContext())
    try:
        await body.look_at(GazeTarget(az=180.0, el=-90.0))
        await asyncio.sleep(0.5)
        state = body.current_face()
        assert 0.8 < state.gaze.x <= 1.0
        assert -1.0 <= state.gaze.y < -0.8
    finally:
        await body.stop()


async def test_nod_and_shake_head_animate_the_face(bus) -> None:
    body = AvatarBody()
    await body.start(BodyContext(bus=bus))
    try:
        nod = await body.gesture("nod", {}, timeout_s=2.0)
        assert nod.status is BehaviorStatus.OK
        nod_amplitude = max(abs(s.gaze.y) for s in body.published)
        assert nod_amplitude > 0.3, "a nod must visibly move the gaze up and down"

        body.published.clear()
        shake = await body.gesture("shake_head", {}, timeout_s=2.0)
        assert shake.status is BehaviorStatus.OK
        assert max(abs(s.gaze.x) for s in body.published) > 0.3
    finally:
        await body.stop()


async def test_express_changes_the_emotion_and_rejects_an_unknown_one() -> None:
    body = AvatarBody()
    await body.start(BodyContext())
    try:
        result = await body.gesture("express", {"emotion": "excited"}, timeout_s=2.0)
        assert result.status is BehaviorStatus.OK
        assert body.current_face().emotion == "excited"

        bad = await body.gesture("express", {"emotion": "smug"}, timeout_s=2.0)
        assert bad.status is BehaviorStatus.ERROR
        assert body.current_face().emotion == "excited"
    finally:
        await body.stop()


async def test_set_face_keeps_lip_and_talking_from_the_core() -> None:
    body = AvatarBody()
    await body.start(BodyContext())
    try:
        await body.set_face(FaceState(emotion="happy", lip=0.7, talking=True))
        state = body.current_face()
        assert (state.emotion, state.lip, state.talking) == ("happy", 0.7, True)
    finally:
        await body.stop()


async def test_stop_all_interrupts_a_running_animation() -> None:
    body = AvatarBody()
    await body.start(BodyContext())
    try:
        gesture = asyncio.create_task(body.gesture("nod", {}, timeout_s=2.0))
        await asyncio.sleep(0.1)
        await body.stop_all("estop")
        result = await gesture
        assert result.status is BehaviorStatus.ERROR
        assert body.last_stop_all_reason == "estop"
    finally:
        await body.stop()


async def test_a_gesture_that_outlives_its_timeout_reports_a_timeout() -> None:
    body = AvatarBody()
    await body.start(BodyContext())
    try:
        result = await body.gesture("nod", {}, timeout_s=0.1)
        assert result.status is BehaviorStatus.TIMEOUT
    finally:
        await body.stop()


async def test_the_avatar_has_no_locomotion() -> None:
    body = AvatarBody()
    result = await body.move(1.0, 0.0, 0.0, 1.0)
    assert result.status is BehaviorStatus.UNSUPPORTED
