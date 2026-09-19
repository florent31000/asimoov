"""Fakes for the Go2 link: a data channel that never touches a robot.

`FakePubSub` mirrors the surface `sport.SportClient` uses from the vendored
`WebRTCDataChannelPubSub`, including the pending-future bookkeeping the
`# ASIMOOV PATCH` in `msgs/future_resolver.py` cleans up.

In its own module, not in `conftest.py`, so `tests/integration` can import it
instead of growing a second copy.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from asimoov.bodies.go2 import sport as sport_mod


class FakePubSub:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.joystick: list[tuple[float, float, float, float, int]] = []
        self.subscriptions: dict[str, Any] = {}
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.hang: set[str] = set()
        self.status_code = 0
        self.mode_name = "normal"
        self.mode_switch_applies = True
        self.obstacle_ok_on_attempt = 1
        self._obstacle_attempts = 0

    async def publish_request_new(self, topic: str, options: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((topic, dict(options)))
        identifier = options["id"]
        if topic in self.hang:
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            self.pending[identifier] = future
            return await future
        return self._reply(topic, options)

    def cancel_request(self, topic: str, identifier: int, msg_type: str | None = None) -> None:
        future = self.pending.pop(identifier, None)
        if future is not None and not future.done():
            future.cancel()

    def publish_without_callback(self, topic: str, data: Any = None, msg_type: Any = None) -> None:
        if topic == sport_mod.TOPIC_WIRELESS_CONTROLLER:
            self.joystick.append(
                (data["lx"], data["ly"], data["rx"], data["ry"], data["keys"])
            )

    def subscribe(self, topic: str, callback: Any = None) -> None:
        self.subscriptions[topic] = callback

    def emit(self, topic: str, data: dict[str, Any]) -> None:
        """Deliver a state message as the real data channel would."""
        callback = self.subscriptions.get(topic)
        if callback is not None:
            callback({"type": "msg", "topic": topic, "data": data})

    def _reply(self, topic: str, options: dict[str, Any]) -> dict[str, Any]:
        code = self.status_code
        payload: dict[str, Any] = {
            "header": {"identity": {"id": options["id"]}, "status": {"code": code}}
        }
        if topic == sport_mod.TOPIC_MOTION_SWITCHER:
            if options["api_id"] == sport_mod.MOTION_SWITCHER_GET:
                payload["data"] = json.dumps({"name": self.mode_name})
            elif self.mode_switch_applies:
                self.mode_name = options["parameter"]["name"]
        if topic == sport_mod.TOPIC_OBSTACLES_AVOID:
            self._obstacle_attempts += 1
            if self._obstacle_attempts < self.obstacle_ok_on_attempt:
                payload["header"]["status"]["code"] = 1
        return {"type": "res", "topic": topic, "data": payload}


class FakeVideo:
    def __init__(self) -> None:
        self.callbacks: list[Any] = []

    def add_track_callback(self, callback: Any) -> None:
        self.callbacks.append(callback)


class FakeDataChannel:
    def __init__(self, pub_sub: FakePubSub) -> None:
        self.pub_sub = pub_sub
        self.video_on: bool | None = None

    def switchVideoChannel(self, switch: bool) -> None:  # noqa: N802 - vendored spelling
        self.video_on = switch


class FakeConnection:
    def __init__(self) -> None:
        self.pub_sub = FakePubSub()
        self.datachannel = FakeDataChannel(self.pub_sub)
        self.video = FakeVideo()
        self.isConnected = True  # noqa: N815 - vendored spelling
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnected = True
        self.isConnected = False


