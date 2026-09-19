"""Unitree Go2 body adapter (WS4, plan.md section 4.8).

Importing this package does not import `aiortc`: the vendored WebRTC driver
is only loaded when `Go2Body` actually opens a connection.
"""

from asimoov.bodies.go2.adapter import Go2Body, gaze_yaw_rx, load_manifest
from asimoov.bodies.go2.motion import JoystickState, MotionController
from asimoov.bodies.go2.sport import ACTION_MAP, SportClient

__all__ = [
    "ACTION_MAP",
    "Go2Body",
    "JoystickState",
    "MotionController",
    "SportClient",
    "gaze_yaw_rx",
    "load_manifest",
]
