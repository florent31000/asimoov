"""Camera sources, selected by the ``camera:`` string in ``robot.yaml``."""

from __future__ import annotations

from asimoov.perception import PerceptionError
from asimoov.perception.camera.base import DEFAULT_FPS, CameraSource, Frame
from asimoov.perception.camera.go2_frames import Go2FrameCamera
from asimoov.perception.camera.opencv import OpenCVCamera
from asimoov.perception.camera.ws_frames import BROWSER_TOPIC, GO2_TOPIC, WsFrameCamera

__all__ = [
    "BROWSER_TOPIC",
    "DEFAULT_FPS",
    "GO2_TOPIC",
    "CameraSource",
    "Frame",
    "Go2FrameCamera",
    "OpenCVCamera",
    "WsFrameCamera",
    "open_camera",
]


def open_camera(spec: str, *, fps: float = DEFAULT_FPS, hub=None) -> CameraSource:
    """Build a camera source from its spec string.

    Accepted specs: ``opencv:N`` (local device N), ``ws_frames`` (any
    ``frame.*`` relayed by the hub), ``go2`` / ``go2_frames`` (``frame.go2``
    only), ``browser`` (``frame.browser`` only).

    Raises:
        PerceptionError: on an unknown spec, or on a hub-fed spec with no hub.
    """
    if spec.startswith("opencv:"):
        index = spec.split(":", 1)[1]
        if not index.isdigit():
            raise PerceptionError(f"camera spec {spec!r} needs a numeric device index")
        return OpenCVCamera(int(index), fps=fps)
    if spec in ("ws_frames", "go2", "go2_frames", "browser"):
        if hub is None:
            raise PerceptionError(f"camera spec {spec!r} needs a hub connection")
        if spec in ("go2", "go2_frames"):
            return Go2FrameCamera(hub, fps=fps)
        if spec == "browser":
            return WsFrameCamera(hub, topic=BROWSER_TOPIC, fps=fps)
        return WsFrameCamera(hub, fps=fps)
    raise PerceptionError(
        f"unknown camera spec {spec!r} (expected opencv:N, ws_frames, browser, or go2)"
    )
