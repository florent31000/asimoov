"""WebFace: throttled broadcast, schema-valid envelopes, page -> bus messages."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from asimoov.contracts.face import FaceGaze, FaceState
from asimoov.faces.server import WebFace, decode_frame, encode_frame_header

SCHEMA_PATH = (
    Path(__file__).parents[2]
    / "src"
    / "asimoov"
    / "contracts"
    / "schemas"
    / "face_state.v1.json"
)
VALIDATOR = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


async def _attach(face: WebFace, page) -> asyncio.Task[None]:
    task = asyncio.create_task(face.attach(page))
    await asyncio.sleep(0)
    return task


async def _detach(page, task: asyncio.Task[None]) -> None:
    page.end()
    await task


async def test_broadcast_is_throttled_and_the_last_state_still_arrives(page) -> None:
    face = WebFace(max_hz=20.0)
    await face.start({})
    task = await _attach(face, page)

    for i in range(10):
        await face.render(FaceState(emotion="happy", intensity=i / 10, lip=i / 10))
    assert len(page.sent) == 1, "ten states in a burst must collapse to one broadcast"

    await asyncio.sleep(0.12)
    assert len(page.sent) == 2
    assert json.loads(page.sent[-1])["data"]["lip"] == pytest.approx(0.9)

    await _detach(page, task)
    await face.stop()


async def test_blink_is_never_dropped(page) -> None:
    face = WebFace(max_hz=20.0)
    await face.start({})
    task = await _attach(face, page)

    await face.render(FaceState(emotion="neutral"))
    await face.render(FaceState(emotion="neutral", blink=True))
    assert len(page.sent) == 2
    assert json.loads(page.sent[-1])["data"]["blink"] is True

    await _detach(page, task)
    await face.stop()


async def test_broadcast_envelope_carries_a_schema_valid_face_state(page) -> None:
    face = WebFace()
    await face.start({})
    task = await _attach(face, page)

    await face.render(
        FaceState(emotion="curious", intensity=0.8, gaze=FaceGaze(0.3, -0.1), lip=0.4, talking=True)
    )
    envelope = json.loads(page.sent[0])
    assert envelope["kind"] == "state"
    assert envelope["topic"] == "face.state"
    assert envelope["src"] == "faces.web"
    VALIDATOR.validate(envelope["data"])

    await _detach(page, task)
    await face.stop()


async def test_a_new_page_gets_the_current_state_immediately(page) -> None:
    face = WebFace()
    await face.start({})
    await face.render(FaceState(emotion="love"))
    task = await _attach(face, page)

    assert json.loads(page.sent[0])["data"]["emotion"] == "love"

    await _detach(page, task)
    await face.stop()


async def test_tap_and_estop_reach_the_bus(page, bus) -> None:
    stops: list[str] = []
    face = WebFace()
    await face.start({"bus": bus, "on_estop": stops.append})
    page.feed(json.dumps({"type": "tap", "intensity": 0.5}))
    page.feed(json.dumps({"type": "estop", "reason": "face_page"}))
    page.end()

    await face.attach(page)

    assert bus.published == [
        ("percept.touched", {"where": "screen", "intensity": 0.5}, "percept"),
        ("safety.estop", {"reason": "face_page"}, "cmd"),
    ]
    assert stops == []  # the bus is the single path; on_estop is the no-bus fallback
    await face.stop()


async def test_estop_falls_back_to_the_hook_without_a_bus(page) -> None:
    stops: list[str] = []
    face = WebFace()
    await face.start({"on_estop": stops.append})
    page.feed(json.dumps({"type": "estop", "reason": "face_page"}))
    page.end()

    await face.attach(page)

    assert stops == ["face_page"]
    await face.stop()


async def test_camera_frames_are_decoded_and_handed_over(page) -> None:
    frames: list[tuple[str, int, str, bytes]] = []
    face = WebFace()
    await face.start({"on_frame": lambda *args: frames.append(args)})
    page.feed(encode_frame_header("frame.browser", 1_648_776_059) + b"\xff\xd8\xffjpeg")
    page.feed(b"short")
    page.end()

    await face.attach(page)

    assert frames == [("frame.browser", 1_648_776_059, "image/jpeg", b"\xff\xd8\xffjpeg")]
    await face.stop()


async def test_frames_are_counted_when_nobody_consumes_them(page) -> None:
    face = WebFace()
    await face.start({})
    page.feed(encode_frame_header("frame.browser", 1000) + b"jpeg")
    page.end()

    await face.attach(page)

    assert face.dropped_frame_count == 1
    await face.stop()


async def test_a_page_without_the_token_is_closed(page) -> None:
    face = WebFace()
    await face.start({"token": "secret"})

    await face.attach(page, path="/face/ws?token=wrong")

    assert page.closed == (1008, "invalid token")
    assert face.connection_count() == 0
    await face.stop()


async def test_metrics_reach_the_debug_hud(page) -> None:
    face = WebFace()
    await face.start({})
    task = await _attach(face, page)

    await face.send_metric("metric.turn_latency_ms", {"p50": 420})
    envelope = json.loads(page.sent[-1])
    assert envelope["kind"] == "metric"
    assert envelope["topic"] == "metric.turn_latency_ms"

    await _detach(page, task)
    await face.stop()


def test_frame_header_round_trip() -> None:
    """The page speaks the frozen codec of `contracts.frames`, nothing local."""
    header = encode_frame_header("frame.browser", 1_648_776_059, seq=3)
    assert len(header) == 8 + len("frame.browser")
    frame = decode_frame(header + b"body")
    assert (frame.topic, frame.seq, frame.ts_ms, frame.jpeg) == (
        "frame.browser",
        3,
        1_648_776_059,
        b"body",
    )


@pytest.mark.parametrize("data", [b"", b"1234567", bytes([9, 1, 0, 0, 0, 0, 0, 0]), bytes(8)])
def test_undecodable_frames_raise(data: bytes) -> None:
    with pytest.raises(ValueError):
        decode_frame(data)
