"""`Go2Body`: the Unitree Go2 as an ASIMOOV `Body` (plan.md section 4.8)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import deque
from pathlib import Path
from typing import Any

import yaml

from asimoov.bodies.go2 import sport as sport_mod
from asimoov.bodies.go2.motion import MOVE_INTERRUPTED, MotionController
from asimoov.bodies.go2.sport import (
    ACTION_MAP,
    ForbiddenCommandError,
    NotConnectedError,
    SportClient,
    UnknownCommandError,
    reply_status,
)
from asimoov.contracts.audio import AudioSink, AudioSource
from asimoov.contracts.behaviors import BehaviorResult
from asimoov.contracts.body import Body, BodyContext, BodyHealth, BodyManifest, GazeTarget
from asimoov.contracts.envelope import new_id
from asimoov.contracts.face import FaceState
from asimoov.contracts.percepts import Battery, BodyState
from asimoov.contracts.vocab import TOPICS
from asimoov.core.timeouts import run_with_timeout

log = logging.getLogger(__name__)

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")

CONNECT_BACKOFF_S: tuple[float, ...] = (5.0, 10.0, 30.0, 60.0)
CONNECT_TIMEOUT_S = 20.0
START_GRACE_S = 0.05
LINK_POLL_S = 1.0
TELEMETRY_PUBLISH_HZ = 2.0

GAZE_DEAD_ZONE_DEG = 8.0
GAZE_FULL_SCALE_DEG = 45.0
GAZE_MAX_RX = 0.3

NO_PRESTAND = frozenset({"StandUp", "StandDown", "RecoveryStand", "Damp"})
POSTURE_BY_COMMAND = {
    "Sit": "sitting",
    "RiseSit": "standing",
    "StandDown": "lying",
    "StandUp": "standing",
    "RecoveryStand": "standing",
    "BalanceStand": "standing",
    "Damp": "lying",
}
MOVING_SPEED_THRESHOLD = 0.05
MAX_ERRORS = 5


def load_manifest(path: Path = MANIFEST_PATH) -> BodyManifest:
    """Load `manifest.yaml` next to this module into a `BodyManifest`."""
    return BodyManifest.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))


class Go2Body(Body):
    """Unitree Go2 over WebRTC.

    Connects in the background: `start` returns immediately (it gives the
    first attempt ``START_GRACE_S`` to land, which an in-process fake link
    always does), then `_connect_loop` retries with the
    ``[5, 10, 30, 60]`` s backoff of plan.md section 4.8. Failures are
    reported through `health`, never raised.

    ``config`` comes from ``robot.yaml``'s ``body.config``:

    ``mode``
        ``sta`` (default) -- the Go2 is on the house Wi-Fi and is found by
        ``serial_number`` (multicast discovery) or by ``robot_ip``.
        ``ap`` -- connect to the robot's own access point (192.168.12.1);
        on Android this needs the process bound to the Wi-Fi network, see
        `android_network`. ``remote`` -- Unitree's TURN relay.
    ``obstacle_avoidance``
        Keep the Go2's own avoidance on (default true).
    ``frames``
        ``{enabled: false, fps: 5}`` -- publish the front camera as
        ``frame.go2`` (see `frames.Go2FrameSource`).

    ``connect`` (constructor argument) is the coroutine function that
    actually builds the link; tests inject one returning a fake connection
    exposing ``datachannel.pub_sub``.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        manifest: BodyManifest | None = None,
        connect: Any = None,
    ) -> None:
        self.manifest = manifest or load_manifest()
        self.config = dict(config or {})
        self._connect = connect or self._default_connect

        limits = self.manifest.limits
        safety = self.manifest.safety
        self._max_speed = float(limits.get("max_speed", 0.6))
        self._max_yaw_rate = float(limits.get("max_yaw_rate", 0.5))
        self._max_continuous_motion_s = float(limits.get("max_continuous_motion_s", 20.0))
        self._forbidden = frozenset(safety.get("forbidden", ()))

        self._ctx: BodyContext | None = None
        self._conn: Any = None
        self._sport: SportClient | None = None
        self._motion: MotionController | None = None
        self._frames: Any = None
        self._connected = False
        self._connected_event = asyncio.Event()
        self._closing = False
        self._errors: deque[str] = deque(maxlen=MAX_ERRORS)
        self._tasks: set[asyncio.Task[Any]] = set()

        self._posture = "unknown"
        self._last_sport_state: dict[str, Any] = {}
        self._last_low_state: dict[str, Any] = {}
        self._published_battery: tuple[float, bool] | None = None
        self._published_body_state: tuple[str, bool] | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self, ctx: BodyContext) -> None:
        self._ctx = ctx
        self._closing = False
        self._connected_event.clear()
        self._spawn(self._connect_loop(), "go2-connect")
        # A grace period, not a requirement: `health().connected` reports the
        # truth either way. `run_with_timeout` so a cancelled start stays
        # cancelled instead of looking like a slow link.
        await run_with_timeout(self._connected_event.wait(), START_GRACE_S)

    async def stop(self) -> None:
        self._closing = True
        await self._teardown_link()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    async def health(self) -> BodyHealth:
        return BodyHealth(
            connected=self._connected,
            battery=self._battery_level(),
            last_rtt_ms=self._sport.last_rtt_ms if self._sport else None,
            errors=tuple(self._errors),
        )

    async def simulate_disconnect(self) -> None:
        """Pretend the link dropped, as `SafetyGuard` conformance expects."""
        await self._on_link_lost("simulated disconnect")

    # -- primitives --------------------------------------------------------

    async def gesture(self, name: str, params: dict[str, Any], *, timeout_s: float) -> BehaviorResult:
        spec = self.manifest.implements.get(name, {})
        primitive = spec.get("primitive", "sport")
        if primitive == "gaze_yaw":
            return BehaviorResult.unsupported(f"{name} is a gaze primitive, call look_at()")
        if primitive != "sport":
            return BehaviorResult.unsupported(f"unknown primitive {primitive!r} for {name}")

        command = spec.get("arg") or ACTION_MAP.get(name)
        if not command:
            return BehaviorResult.unsupported(f"no sport command for gesture {name!r}")
        if command in self._forbidden:
            return BehaviorResult.error(f"{command} is forbidden by the body manifest")
        if not self._connected or self._sport is None:
            return BehaviorResult.error("not connected")

        deadline = time.monotonic() + timeout_s
        try:
            if command not in NO_PRESTAND and self._posture != "standing":
                # The Go2 ignores most sport commands while sitting or lying
                # (Neon `_stand_up_first`), but sending RecoveryStand before
                # every single gesture jerks the robot for nothing.
                await self._sport.sport(
                    "RecoveryStand", timeout_s=self._remaining(deadline, timeout_s)
                )
                self._posture = "standing"
                await asyncio.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
            reply = await self._sport.sport(
                command, timeout_s=self._remaining(deadline, timeout_s)
            )
        except ForbiddenCommandError as exc:
            return BehaviorResult.error(str(exc))
        except (UnknownCommandError, NotConnectedError) as exc:
            return BehaviorResult.error(str(exc))
        except asyncio.TimeoutError:
            self._record_error(f"{command} timed out after {timeout_s}s")
            return BehaviorResult.timeout(f"{command} got no reply in {timeout_s}s")

        code = reply_status(reply)
        if code > 0:
            self._record_error(f"{command} refused with status {code}")
            return BehaviorResult.error(f"{command} refused by the robot (status {code})")
        self._posture = POSTURE_BY_COMMAND.get(command, self._posture)
        return BehaviorResult.ok(command=command)

    async def move(self, vx: float, vy: float, wz: float, duration_s: float) -> BehaviorResult:
        if not self._connected or self._motion is None:
            return BehaviorResult.error("not connected")
        effective = self._motion.move(vx, vy, wz, duration_s)
        action_id = new_id()
        if effective > 0:
            self._spawn(
                self._report_move_done(action_id, effective, self._motion.command_id),
                "go2-move-done",
            )
        return BehaviorResult.started(action_id, eta_s=effective)

    async def look_at(self, target: GazeTarget) -> None:
        if self._motion is None or self._motion.is_moving():
            return
        self._motion.set_gaze_yaw(gaze_yaw_rx(target.az))

    async def set_face(self, state: FaceState) -> None:
        return None

    async def stop_all(self, reason: str) -> None:
        log.info("go2 stop_all: %s", reason)
        if self._motion is not None:
            await self._motion.stop_all()

    def audio_source(self) -> AudioSource | None:
        return None

    def audio_sink(self) -> AudioSink | None:
        return None

    # -- connection --------------------------------------------------------

    async def _connect_loop(self) -> None:
        attempt = 0
        while not self._closing:
            try:
                connected, conn = await run_with_timeout(
                    self._connect(self.config),
                    self.config.get("connect_timeout_s", CONNECT_TIMEOUT_S),
                )
                if not connected:
                    self._record_error("connection attempt timed out")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reported via health(), never raised
                self._record_error(f"connection failed: {exc}")
                conn = None

            if conn is not None:
                try:
                    await self._on_connected(conn)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._record_error(f"init failed: {exc}")
                    await self._teardown_link()

            delay = CONNECT_BACKOFF_S[min(attempt, len(CONNECT_BACKOFF_S) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    async def _on_connected(self, conn: Any) -> None:
        self._conn = conn
        self._sport = SportClient(
            conn.datachannel.pub_sub,
            forbidden=self._forbidden,
        )
        self._motion = MotionController(
            self._sport.send_joystick,
            stop_move=self._send_stop_move,
            max_speed=self._max_speed,
            max_yaw_rate=self._max_yaw_rate,
            max_continuous_motion_s=self._max_continuous_motion_s,
            watchdog_s=float(self.manifest.safety.get("watchdog_ms", 500)) / 1000.0,
            on_error=self._record_error,
        )
        await self._init()
        await self._motion.start()
        self._connected = True
        self._connected_event.set()
        self._spawn(self._link_monitor(), "go2-link-monitor")
        self._spawn(self._telemetry_loop(), "go2-telemetry")
        await self._start_frames()

    async def _init(self) -> None:
        """Motion mode and obstacle avoidance (Neon controller.py l.329-388).

        Both are best-effort: a robot that refuses the mode switch is still
        usable, so the failure is recorded in `health` rather than aborting
        the connection.
        """
        assert self._sport is not None
        try:
            if not await self._sport.ensure_normal_mode():
                self._record_error("motion mode is still not normal after the switch")
        except (asyncio.TimeoutError, sport_mod.SportError) as exc:
            self._record_error(f"motion mode switch failed: {exc}")

        if self.config.get("obstacle_avoidance", True):
            try:
                if not await self._sport.set_obstacle_avoidance(True):
                    self._record_error("obstacle avoidance not acknowledged")
            except (asyncio.TimeoutError, sport_mod.SportError) as exc:
                self._record_error(f"obstacle avoidance failed: {exc}")

        self._sport.subscribe(sport_mod.TOPIC_SPORT_MODE_STATE, self._on_sport_state)
        self._sport.subscribe(sport_mod.TOPIC_LOW_STATE, self._on_low_state)

    async def _start_frames(self) -> None:
        frames_cfg = self.config.get("frames") or {}
        if not frames_cfg.get("enabled"):
            return
        send_frame = getattr(self._ctx.bus, "publish_frame", None) if self._ctx else None
        if send_frame is None:
            # Binary frames need a bus with a frame channel (`LocalBus`,
            # `BusClient`). Saying so beats publishing JPEGs nobody receives.
            self._record_error("frames disabled: this bus carries no binary frames")
            return
        from asimoov.bodies.go2.frames import Go2FrameSource

        self._frames = Go2FrameSource(
            self._conn,
            send_frame=send_frame,
            fps=float(frames_cfg.get("fps", 5.0)),
        )
        try:
            await self._frames.start()
        except Exception as exc:  # noqa: BLE001 - camera is optional
            self._record_error(f"frames disabled: {exc}")
            self._frames = None

    async def _link_monitor(self) -> None:
        while not self._closing:
            await asyncio.sleep(LINK_POLL_S)
            if self._conn is None:
                continue
            if not getattr(self._conn, "isConnected", True):
                await self._on_link_lost("webrtc link down")
                return

    async def _on_link_lost(self, reason: str) -> None:
        if not self._connected:
            return
        self._record_error(reason)
        await self.stop_all(reason)
        await self._teardown_link()
        if not self._closing:
            self._spawn(self._connect_loop(), "go2-reconnect")

    async def _teardown_link(self) -> None:
        self._connected = False
        self._connected_event.clear()
        if self._frames is not None:
            with contextlib.suppress(Exception):
                await self._frames.stop()
            self._frames = None
        if self._motion is not None:
            await self._motion.stop()
            self._motion = None
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.disconnect()
            self._conn = None
        self._sport = None

    async def _send_stop_move(self) -> None:
        if self._sport is None:
            return
        with contextlib.suppress(asyncio.TimeoutError, sport_mod.SportError):
            await self._sport.sport("StopMove", timeout_s=1.0)

    # -- telemetry ---------------------------------------------------------

    def _on_sport_state(self, message: dict[str, Any]) -> None:
        data = message.get("data")
        if isinstance(data, dict):
            self._last_sport_state = data

    def _on_low_state(self, message: dict[str, Any]) -> None:
        data = message.get("data")
        if isinstance(data, dict):
            self._last_low_state = data

    def _battery_level(self) -> float | None:
        bms = self._last_low_state.get("bms_state")
        if not isinstance(bms, dict) or "soc" not in bms:
            return None
        return max(0.0, min(1.0, float(bms["soc"]) / 100.0))

    def _battery_charging(self) -> bool:
        bms = self._last_low_state.get("bms_state")
        if not isinstance(bms, dict):
            return False
        # The Go2 reports pack current signed: positive while charging.
        return float(bms.get("current", 0.0)) > 0.0

    def _is_moving(self) -> bool:
        velocity = self._last_sport_state.get("velocity")
        if isinstance(velocity, (list, tuple)):
            return any(abs(float(v)) > MOVING_SPEED_THRESHOLD for v in velocity)
        return bool(self._motion and self._motion.is_moving())

    async def _telemetry_loop(self) -> None:
        """Turn the robot's state topics into `battery` / `body_state` percepts.

        Publishes on change only, at most `TELEMETRY_PUBLISH_HZ` times per
        second: ``rt/lf/sportmodestate`` and ``rt/lf/lowstate`` arrive far
        faster than the bus needs them.
        """
        period = 1.0 / TELEMETRY_PUBLISH_HZ
        while not self._closing:
            await asyncio.sleep(period)
            level = self._battery_level()
            if level is not None:
                battery = (round(level, 2), self._battery_charging())
                if battery != self._published_battery:
                    self._published_battery = battery
                    await self._publish(
                        f"{TOPICS.PERCEPT_PREFIX}{Battery.PERCEPT_TYPE}",
                        Battery(level=battery[0], charging=battery[1]).to_dict(),
                    )

            body_state = (self._posture, self._is_moving())
            if body_state != self._published_body_state:
                self._published_body_state = body_state
                await self._publish(
                    f"{TOPICS.PERCEPT_PREFIX}{BodyState.PERCEPT_TYPE}",
                    BodyState(posture=body_state[0], moving=body_state[1]).to_dict(),
                )

    async def _report_move_done(self, action_id: str, eta_s: float, command_id: int) -> None:
        await asyncio.sleep(eta_s)
        if self._motion is None:
            status, reason = MOVE_INTERRUPTED, "link lost"
        else:
            status, reason = self._motion.outcome(command_id)
        await self._publish(
            TOPICS.BODY_REPLY,
            {"action_id": action_id, "name": "move", "status": status, "reason": reason},
            kind="reply",
        )

    async def _publish(self, topic: str, data: dict[str, Any], kind: str = "percept") -> None:
        ctx = self._ctx
        if ctx is None:
            return
        if ctx.bus is not None:
            await ctx.bus.publish(topic, data, kind=kind)
            return
        if ctx.publish is None:
            return
        result = ctx.publish(topic, data, kind=kind)
        if asyncio.iscoroutine(result):
            await result

    # -- helpers -----------------------------------------------------------

    def _spawn(self, coro: Any, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _record_error(self, message: str) -> None:
        log.warning("go2: %s", message)
        self._errors.append(message)

    @staticmethod
    def _remaining(deadline: float, timeout_s: float) -> float:
        return max(0.05, min(timeout_s, deadline - time.monotonic()))

    async def _default_connect(self, config: dict[str, Any]) -> Any:
        """Build a real WebRTC connection with the vendored driver.

        Imported lazily: `aiortc` only has to be installed when a Go2 is
        actually used (``pip install asimoov[go2]``).
        """
        from asimoov.bodies.go2.vendor.unitree_webrtc_connect.constants import (
            WebRTCConnectionMethod,
        )
        from asimoov.bodies.go2.vendor.unitree_webrtc_connect.webrtc_driver import (
            UnitreeWebRTCConnection,
        )

        mode = str(config.get("mode", "sta")).lower()
        serial = config.get("serial_number") or ""
        ip = config.get("robot_ip") or ""

        if mode == "remote":
            conn = UnitreeWebRTCConnection(
                WebRTCConnectionMethod.Remote,
                serialNumber=serial,
                username=config.get("remote_username"),
                password=config.get("remote_password"),
            )
        elif mode == "ap":
            conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalAP)
        elif serial and not ip:
            conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, serialNumber=serial)
        elif ip:
            conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=ip)
        else:
            raise ValueError("go2 sta mode needs a serial_number or a robot_ip")

        if mode == "ap":
            from asimoov.bodies.go2 import android_network

            android_network.bind_to_wifi()
            try:
                await conn.connect()
            finally:
                android_network.unbind()
        else:
            await conn.connect()
        return conn


def gaze_yaw_rx(az: float) -> float:
    """Joystick yaw for a gaze azimuth, in the Go2's frame.

    ``az`` follows `GazeTarget.az`: positive = the robot's own left. The Go2
    yaws left for a *negative* ``rx``, hence the sign flip. Below
    `GAZE_DEAD_ZONE_DEG` the robot does not move at all (the head is a
    camera on a rigid body: every gaze correction rotates the whole dog);
    the response then grows linearly to `GAZE_MAX_RX` at
    `GAZE_FULL_SCALE_DEG`.
    """
    magnitude = abs(az)
    if magnitude <= GAZE_DEAD_ZONE_DEG:
        return 0.0
    span = GAZE_FULL_SCALE_DEG - GAZE_DEAD_ZONE_DEG
    scaled = min((magnitude - GAZE_DEAD_ZONE_DEG) / span, 1.0) * GAZE_MAX_RX
    return -math.copysign(scaled, az)
