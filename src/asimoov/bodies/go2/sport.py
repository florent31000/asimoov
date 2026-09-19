"""Sport-mode commands over the Go2 data channel, with a real timeout.

Neon called ``pub_sub.publish_request_new`` and awaited it forever: a lost
reply hung the caller and left a future in the resolver (plan.md section 2,
bug 7). `SportClient.request` bounds every request with
`core.timeouts.run_with_timeout` and drops the pending entry when it fires.

Topic names and api ids mirror `vendor/unitree_webrtc_connect/constants.py`;
they are repeated here because importing that module pulls in `aiortc` (the
vendored package's ``__init__`` monkey-patches it), which must not be
required to import this adapter. ``tests/bodies/go2/test_sport.py`` parses
the vendored file and asserts the two stay in sync.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from asimoov.core.timeouts import run_with_timeout

TOPIC_SPORT = "rt/api/sport/request"
TOPIC_MOTION_SWITCHER = "rt/api/motion_switcher/request"
TOPIC_OBSTACLES_AVOID = "rt/api/obstacles_avoid/request"
TOPIC_WIRELESS_CONTROLLER = "rt/wirelesscontroller"
TOPIC_SPORT_MODE_STATE = "rt/lf/sportmodestate"
TOPIC_LOW_STATE = "rt/lf/lowstate"

SPORT_CMD: dict[str, int] = {
    "Damp": 1001,
    "BalanceStand": 1002,
    "StopMove": 1003,
    "StandUp": 1004,
    "StandDown": 1005,
    "RecoveryStand": 1006,
    "Sit": 1009,
    "RiseSit": 1010,
    "Hello": 1016,
    "Stretch": 1017,
    "Content": 1020,
    "Dance1": 1022,
    "Dance2": 1023,
    "Scrape": 1029,
    "FrontFlip": 1030,
    "FrontJump": 1031,
    "FrontPounce": 1032,
    "WiggleHips": 1033,
    "FingerHeart": 1036,
    "LeftFlip": 1042,
    "RightFlip": 1043,
    "BackFlip": 1044,
    "Handstand": 1301,
}

# Neon `src/robot/controller.py` l.41-49, plus "dance" (Neon picked Dance1 or
# Dance2 at random in `do_action`; the adapter's manifest decides instead).
ACTION_MAP: dict[str, str] = {
    "stand_up": "StandUp",
    "sit": "Sit",
    "lie_down": "StandDown",
    "wave_hello": "Hello",
    "stretch": "Stretch",
    "heart": "FingerHeart",
    "wiggle": "WiggleHips",
    "dance": "Dance1",
}

MOTION_SWITCHER_GET = 1001
MOTION_SWITCHER_SET = 1002
OBSTACLES_AVOID_SET = 1002

# The Go2 needs a moment to actually be in normal mode after the switch.
MODE_SWITCH_SETTLE_S = 2.0

_ID_MODULO = 2147483648


class SportError(Exception):
    """Base error for sport-mode commands."""


class ForbiddenCommandError(SportError):
    """The command is listed in ``BodyManifest.safety.forbidden``."""


class UnknownCommandError(SportError):
    """No such sport command in `SPORT_CMD`."""


class NotConnectedError(SportError):
    """The data channel is not available."""


def reply_status(reply: dict[str, Any] | None) -> int:
    """Return the ``header.status.code`` of a reply, or -1 if absent."""
    if not reply or not reply.get("data"):
        return -1
    return reply["data"].get("header", {}).get("status", {}).get("code", -1)


class SportClient:
    """Request/response and fire-and-forget traffic on the Go2 data channel.

    ``pub_sub`` is the vendored `WebRTCDataChannelPubSub` (or any object with
    the same ``publish_request_new`` / ``publish_without_callback`` /
    ``cancel_request`` surface, which is what the tests inject).
    """

    def __init__(
        self,
        pub_sub: Any,
        *,
        forbidden: tuple[str, ...] | frozenset[str] = (),
        default_timeout_s: float = 3.0,
    ) -> None:
        self._pub_sub = pub_sub
        self._forbidden = frozenset(forbidden)
        self._default_timeout_s = default_timeout_s
        self._next_id = int(time.time() * 1000) % _ID_MODULO
        self.last_rtt_ms: float | None = None

    @property
    def forbidden(self) -> frozenset[str]:
        return self._forbidden

    def _new_id(self) -> int:
        self._next_id = (self._next_id + 1) % _ID_MODULO
        return self._next_id

    async def request(
        self, topic: str, payload: dict[str, Any], timeout_s: float = 3.0
    ) -> dict[str, Any]:
        """Send a request on ``topic`` and wait up to ``timeout_s`` for its reply.

        Raises:
            asyncio.TimeoutError: if no reply arrives in time. The pending
                future is removed from the vendored resolver first, so a late
                reply is discarded instead of resolving a dead request.
            NotConnectedError: if there is no data channel.
        """
        if self._pub_sub is None:
            raise NotConnectedError("no data channel")

        options = dict(payload)
        options["id"] = self._new_id()
        started = time.monotonic()
        answered, reply = await run_with_timeout(
            self._pub_sub.publish_request_new(topic, options), timeout_s
        )
        if not answered:
            self._pub_sub.cancel_request(topic, options["id"])
            raise asyncio.TimeoutError(f"{topic} did not reply within {timeout_s}s")
        self.last_rtt_ms = (time.monotonic() - started) * 1000.0
        return reply

    async def sport(
        self, name: str, parameter: Any = None, *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        """Run the named sport command (`SPORT_CMD`) and wait for its reply.

        Raises:
            ForbiddenCommandError: if ``name`` is in `forbidden`.
            UnknownCommandError: if ``name`` is not a known sport command.
            asyncio.TimeoutError: see `request`.
        """
        if name in self._forbidden:
            raise ForbiddenCommandError(f"{name} is forbidden by the body manifest")
        api_id = SPORT_CMD.get(name)
        if api_id is None:
            raise UnknownCommandError(f"unknown sport command: {name}")

        payload: dict[str, Any] = {"api_id": api_id}
        if parameter is not None:
            payload["parameter"] = parameter
        return await self.request(
            TOPIC_SPORT, payload, self._default_timeout_s if timeout_s is None else timeout_s
        )

    def send_joystick(self, lx: float, ly: float, rx: float, ry: float, keys: int = 0) -> None:
        """Emulate one wireless-controller frame (Neon controller.py l.189-250).

        Going through ``rt/wirelesscontroller`` rather than the ``Move`` sport
        command is what keeps the Go2's own obstacle avoidance in the loop.
        Fire-and-forget: the robot never answers this topic.
        """
        if self._pub_sub is None:
            return
        self._pub_sub.publish_without_callback(
            TOPIC_WIRELESS_CONTROLLER,
            {
                "lx": float(lx),
                "ly": float(ly),
                "rx": float(rx),
                "ry": float(ry),
                "keys": int(keys),
            },
        )

    def subscribe(self, topic: str, callback: Any) -> None:
        if self._pub_sub is None:
            return
        self._pub_sub.subscribe(topic, callback)

    async def ensure_normal_mode(
        self, *, timeout_s: float = 3.0, settle_s: float | None = None
    ) -> bool:
        """Switch the motion mode to ``normal`` if it is not already (l.329-355).

        Returns True if the robot is in normal mode when this returns: the
        switch is acknowledged before the robot has applied it, so the mode
        is read back after the settle delay rather than assumed.
        """
        reply = await self.request(
            TOPIC_MOTION_SWITCHER, {"api_id": MOTION_SWITCHER_GET}, timeout_s
        )
        if _mode_name(reply) == "normal":
            return True

        await self.request(
            TOPIC_MOTION_SWITCHER,
            {"api_id": MOTION_SWITCHER_SET, "parameter": {"name": "normal"}},
            timeout_s,
        )
        await asyncio.sleep(MODE_SWITCH_SETTLE_S if settle_s is None else settle_s)
        confirmed = await self.request(
            TOPIC_MOTION_SWITCHER, {"api_id": MOTION_SWITCHER_GET}, timeout_s
        )
        return _mode_name(confirmed) == "normal"

    async def set_obstacle_avoidance(self, enabled: bool, *, timeout_s: float = 3.0) -> bool:
        """Enable/disable the Go2's obstacle avoidance (l.357-388).

        Firmware versions disagree on the payload shape, so the three known
        forms are tried in order; returns True as soon as one is acknowledged
        with status 0.
        """
        payloads: list[dict[str, Any]] = [
            {"api_id": OBSTACLES_AVOID_SET, "parameter": enabled},
            {"api_id": OBSTACLES_AVOID_SET, "parameter": {"switch": enabled}},
            {"api_id": OBSTACLES_AVOID_SET, "parameter": {"data": enabled}},
        ]
        for payload in payloads:
            reply = await self.request(TOPIC_OBSTACLES_AVOID, payload, timeout_s)
            if reply_status(reply) == 0:
                return True
        return False


def _mode_name(reply: dict[str, Any] | None) -> str | None:
    if not reply or not reply.get("data"):
        return None
    data = reply["data"].get("data")
    if not data:
        return None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    return data.get("name") if isinstance(data, dict) else None
