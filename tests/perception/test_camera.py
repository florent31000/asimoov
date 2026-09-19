"""Camera sources: the spec factory and the hub-fed frame source."""

from __future__ import annotations

import asyncio

import pytest
from helpers import synthetic_image

from asimoov.contracts.frames import encode_frame
from asimoov.perception import PerceptionError
from asimoov.perception.camera import Go2FrameCamera, OpenCVCamera, WsFrameCamera, open_camera
from asimoov.perception.camera.ws_frames import BROWSER_TOPIC, GO2_TOPIC

cv2 = pytest.importorskip("cv2", reason="needs the [vision] extra")


class DummyHub:
    """The slice of `core.bus.BusClient` a hub-fed camera uses."""

    def __init__(self) -> None:
        self.handler = None

    def subscribe_frames(self, handler) -> DummyFrameSubscription:
        self.handler = handler
        return DummyFrameSubscription(self)


class DummyFrameSubscription:
    def __init__(self, hub: DummyHub) -> None:
        self._hub = hub

    def unsubscribe(self) -> None:
        self._hub.handler = None


def jpeg_of(width: int = 320, height: int = 180) -> bytes:
    ok, buffer = cv2.imencode(".jpg", synthetic_image(width, height, seed=3))
    assert ok
    return buffer.tobytes()


def test_opencv_spec():
    camera = open_camera("opencv:2")
    assert isinstance(camera, OpenCVCamera)
    assert camera.index == 2


def test_hub_specs():
    hub = DummyHub()
    assert isinstance(open_camera("ws_frames", hub=hub), WsFrameCamera)
    assert isinstance(open_camera("go2", hub=hub), Go2FrameCamera)
    assert isinstance(open_camera("go2_frames", hub=hub), Go2FrameCamera)
    assert open_camera("browser", hub=hub).topic == BROWSER_TOPIC
    assert open_camera("go2", hub=hub).topic == GO2_TOPIC


def test_a_hub_spec_without_a_hub_is_an_error():
    with pytest.raises(PerceptionError, match="needs a hub"):
        open_camera("ws_frames")


def test_an_unknown_spec_is_an_error():
    with pytest.raises(PerceptionError, match="unknown camera spec"):
        open_camera("webcam0")
    with pytest.raises(PerceptionError, match="numeric device index"):
        open_camera("opencv:front")


async def test_ws_frames_decodes_a_hub_frame():
    hub = DummyHub()
    camera = WsFrameCamera(hub, fps=1000)
    await camera.start()
    assert hub.handler == camera.on_binary
    await camera.on_binary(encode_frame(BROWSER_TOPIC, jpeg_of()))
    frame = await asyncio.wait_for(camera.read(), 2.0)
    assert frame.width == 320
    assert frame.height == 180
    await camera.stop()
    assert hub.handler is None


async def test_ws_frames_ignores_other_topics():
    camera = Go2FrameCamera(DummyHub(), fps=1000)
    await camera.start()
    await camera.on_binary(encode_frame(BROWSER_TOPIC, jpeg_of()))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(camera.read(), 0.2)
    await camera.stop()


async def test_only_the_newest_frame_is_kept():
    camera = WsFrameCamera(DummyHub(), fps=1000)
    await camera.start()
    for _ in range(3):
        await camera.on_binary(encode_frame(BROWSER_TOPIC, jpeg_of()))
    assert camera.dropped == 2
    await asyncio.wait_for(camera.read(), 2.0)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(camera.read(), 0.2)
    await camera.stop()


async def test_an_undecodable_message_is_dropped_not_raised():
    camera = WsFrameCamera(DummyHub(), fps=1000)
    await camera.start()
    await camera.on_binary(b"\x01")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(camera.read(), 0.2)
    await camera.stop()


async def test_reading_a_stopped_source_ends_the_stream():
    camera = WsFrameCamera(DummyHub(), fps=1000)
    await camera.start()
    await camera.stop()
    assert await camera.read() is None


async def test_opening_a_missing_device_is_an_explicit_error():
    camera = OpenCVCamera(97)
    with pytest.raises(PerceptionError, match="cannot open camera"):
        await camera.start()
