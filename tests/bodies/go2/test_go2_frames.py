"""`Go2FrameSource`: the camera track throttled down to `frame.go2` messages."""

from __future__ import annotations

import asyncio

import pytest

from asimoov.bodies.go2.frames import Go2FrameSource
from asimoov.contracts.frames import TOPIC_FRAME_GO2, decode_frame


class FrameSink(list):
    """The bus frame channel: ``await send_frame(bytes)``."""

    async def __call__(self, frame: bytes) -> None:
        self.append(frame)


@pytest.fixture
def sent() -> FrameSink:
    return FrameSink()


class FakeFrame:
    width = 1280
    height = 720


class FakeTrack:
    """Delivers ``count`` frames, then blocks like a live track would."""

    def __init__(self, count: int) -> None:
        self.remaining = count

    async def recv(self) -> FakeFrame:
        if self.remaining <= 0:
            await asyncio.Event().wait()
        self.remaining -= 1
        return FakeFrame()


class FakeClock:
    """One tick of 20 ms per frame: 10 frames span 200 ms."""

    def __init__(self, step: float = 0.02) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


async def drain(source: Go2FrameSource, expected: int) -> None:
    for _ in range(200):
        if source.published >= expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"only {source.published} frames published, expected {expected}")


async def test_frames_are_throttled_to_the_requested_fps(connection, sent) -> None:
    source = Go2FrameSource(
        connection,
        send_frame=sent,
        fps=10.0,  # one frame every 100 ms, i.e. one in five
        encode=lambda frame: b"jpeg-bytes",
        clock=FakeClock(),
    )
    await source.start()
    assert connection.datachannel.video_on is True
    assert connection.video.callbacks

    await connection.video.callbacks[0](FakeTrack(10))
    await drain(source, 2)
    await source.stop()

    assert source.published == 2, "5 of every 10 frames must be dropped at 10 fps"
    frame = decode_frame(sent[0])
    assert (frame.topic, frame.seq, frame.jpeg) == (TOPIC_FRAME_GO2, 1, b"jpeg-bytes")
    assert connection.datachannel.video_on is False


async def test_a_single_reader_task_per_track(connection, sent) -> None:
    source = Go2FrameSource(
        connection,
        send_frame=sent,
        fps=10.0,
        encode=lambda frame: b"x",
        clock=FakeClock(),
    )
    await source.start()
    await connection.video.callbacks[0](FakeTrack(4))
    await connection.video.callbacks[0](FakeTrack(4))
    assert len([t for t in asyncio.all_tasks() if t.get_name() == "go2-frames"]) == 1
    await source.stop()


async def test_an_unencodable_frame_does_not_kill_the_stream(connection, sent) -> None:
    calls = {"n": 0}

    def encode(frame: object) -> bytes:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("bad frame")
        return b"ok"

    source = Go2FrameSource(
        connection, send_frame=sent, fps=10.0, encode=encode, clock=FakeClock()
    )
    await source.start()
    await connection.video.callbacks[0](FakeTrack(10))
    await drain(source, 1)
    await source.stop()

    assert decode_frame(sent[0]).jpeg == b"ok"
