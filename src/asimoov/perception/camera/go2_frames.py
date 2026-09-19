"""Go2 camera: the same hub frame stream, restricted to ``frame.go2``.

WS4's Go2 adapter owns the WebRTC video track and republishes it as binary
``frame.go2`` messages; perception is a plain consumer.
"""

from __future__ import annotations

from asimoov.perception.camera.base import DEFAULT_FPS
from asimoov.perception.camera.ws_frames import GO2_TOPIC, WsFrameCamera


class Go2FrameCamera(WsFrameCamera):
    """``WsFrameCamera`` pinned to the ``frame.go2`` topic."""

    def __init__(self, hub, *, fps: float = DEFAULT_FPS) -> None:
        super().__init__(hub, topic=GO2_TOPIC, fps=fps)
