"""`InMoovBody`: the `Body` adapter for the InMoov bust (and the finger bench)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from asimoov.bodies.inmoov.faults import FaultTracker
from asimoov.bodies.inmoov.gaze_loop import GazeLoop
from asimoov.bodies.inmoov.gestures import Gesture, GestureError, GestureRunner, load_gestures
from asimoov.bodies.inmoov.jaw import JawDriver
from asimoov.bodies.inmoov.link import ChannelSpec, Link, SerialLink, TcpLink, parse_channels
from asimoov.bodies.inmoov.servo_face import ServoFace
from asimoov.contracts.audio import AudioSink, AudioSource
from asimoov.contracts.behaviors import BehaviorResult
from asimoov.contracts.body import Body, BodyContext, BodyHealth, BodyManifest, GazeTarget
from asimoov.contracts.envelope import new_id
from asimoov.contracts.face import FaceState
from asimoov.contracts.vocab import TOPICS, is_capability
from asimoov.core.timeouts import run_with_timeout

LOG = logging.getLogger(__name__)

GESTURES_DIR = Path(__file__).parent / "gestures"
START_CONNECT_GRACE_S = 0.09
STOP_ALL_TIMEOUT_S = 0.15
SHORT_GESTURE_MAX_MS = 1500

# A channel name is what tells the adapter which capability the bust has.
CHANNEL_CAPABILITIES = {
    "fingers_r": "gesture.hand_right",
    "fingers_l": "gesture.hand_left",
    "neck_yaw": "gaze.pan_tilt",
    "jaw": "face.jaw",
    "eyelids": "face.eyelids",
}

# Driven by the always-on loops, which never attach anything themselves: a
# gesture is what attaches its own joints, the head is attached on connect or
# it stays limp forever.
CONTINUOUS_CHANNELS = ("neck_yaw", "neck_pitch", "jaw", "eyelids")

_BASE_MANIFEST = BodyManifest(
    name="inmoov",
    kind_of_body="humanoid_bust",
    capabilities=(),
    implements={},
    limits={"max_continuous_motion_s": 20},
    safety={"watchdog_ms": 2000, "stop_on_disconnect": True, "forbidden": []},
)


class InMoovBody(Body):
    """Drives the InMoov firmware over serial or TCP.

    The manifest is empty until the link comes up: capabilities, gestures
    and limits all come from the firmware's ``V`` answer, so the same
    adapter serves the one-finger bench and a full bust without a config
    file describing the hardware twice.

    ``config`` keys: ``link`` (``serial`` or ``tcp``), ``port`` (serial port
    or TCP port), ``host``, ``limits`` (``{joint: [min, max]}``, tightened
    on the firmware at every connect), ``gestures_dir``.
    """

    def __init__(self, config: dict[str, Any] | None = None, *, link: Link | None = None) -> None:
        self._config = dict(config or {})
        self.manifest = _BASE_MANIFEST
        self._link = link if link is not None else self._build_link(self._config)
        self._link.set_callbacks(on_connect=self._on_connect, on_disconnect=self._on_disconnect)
        self._channels: dict[str, ChannelSpec] = {}
        self._gestures: dict[str, Gesture] = load_gestures(
            GESTURES_DIR, *_extra_dirs(self._config)
        )
        self._runner = GestureRunner(self._link, self._channels)
        self._gaze = GazeLoop(self._link, self._channels)
        self._face = ServoFace(self._link, self._channels)
        self._jaw: JawDriver | None = None
        self._ready = asyncio.Event()
        self._action_task: asyncio.Task[None] | None = None
        self._ctx: BodyContext | None = None
        self._errors: tuple[str, ...] = ()
        self._tasks: set[asyncio.Task] = set()
        self._faults = FaultTracker()

    def _build_link(self, config: dict[str, Any]) -> Link:
        kind = config.get("link", "serial")
        if kind == "tcp":
            return TcpLink(config.get("host", "192.168.1.50"), int(config.get("port", 5005)))
        if kind == "serial":
            return SerialLink(config.get("port", "COM5"))
        raise ValueError(f"unknown inmoov link {kind!r}, expected 'serial' or 'tcp'")

    # -- lifecycle ----------------------------------------------------------

    async def start(self, ctx: BodyContext) -> None:
        self._ctx = ctx
        await self._link.start()
        # Give a fast link (USB, LAN) the chance to be up on return without
        # ever blocking past the 100 ms the contract allows; a slow one just
        # reports connected=False until its retry loop succeeds.
        await run_with_timeout(self._ready.wait(), START_CONNECT_GRACE_S)

    async def stop(self) -> None:
        await self._cancel_action()
        await self._gaze.stop()
        await self._face.stop()
        if self._link.connected:
            await self._link.send("D all", timeout_s=0.3)
        await self._link.close()
        self._ready.clear()

    async def health(self) -> BodyHealth:
        errors = self._errors + self._faults.errors
        if self._link.estopped:
            errors += ("ERR estop",)
        return BodyHealth(
            connected=self._link.connected and self._ready.is_set(),
            battery=None,
            last_rtt_ms=self._link.last_rtt_ms,
            errors=errors,
        )

    async def _on_connect(self) -> None:
        self._errors = ()
        reply = await self._link.send("V")
        if reply.status != "END":
            self._errors = (f"V refused: {reply.error}",)
            LOG.warning("inmoov: no channel list (%s)", reply.error)
            return
        self._channels.clear()
        self._channels.update(parse_channels(reply.lines))

        tightened = False
        for joint, bounds in (self._config.get("limits") or {}).items():
            spec = self._channels.get(joint)
            if spec is None:
                continue
            await self._link.send(f"L {spec.id} {int(bounds[0])} {int(bounds[1])}")
            tightened = True
        if tightened:
            # Re-read so both sides agree on the limits now in force.
            reply = await self._link.send("V")
            if reply.status == "END":
                self._channels.clear()
                self._channels.update(parse_channels(reply.lines))

        for name in CONTINUOUS_CHANNELS:
            spec = self._channels.get(name)
            if spec is None:
                continue
            attach = await self._link.send(f"E {spec.id}")
            if not attach.ok:
                self._errors += (f"E {name} refused: {attach.error}",)
                LOG.warning("inmoov: %s stays detached (%s)", name, attach.error)

        self._faults.reset()
        self._gaze = GazeLoop(self._link, self._channels, faults=self._faults)
        self._face = ServoFace(self._link, self._channels, faults=self._faults)
        self._jaw = (
            JawDriver(self._link, faults=self._faults) if "jaw" in self._channels else None
        )
        self._refresh_manifest()
        self._publish_manifest()
        await self._face.start({})
        await self._gaze.start()
        self._ready.set()

    def _publish_manifest(self) -> None:
        """Tell the core what this bust turned out to be.

        The channel table comes from the firmware, so before the link is up
        the manifest declares nothing and the core's resolver hides every
        gesture the bust can do. Republishing here, on every connect and
        every reconnect, is how the core rebinds (review, major 11).
        """
        self._publish(TOPICS.BODY_MANIFEST, self.manifest.to_dict(), kind="state")

    async def _on_disconnect(self) -> None:
        self._ready.clear()
        self._errors = ("link lost",)
        await self._gaze.stop()
        await self.stop_all("link_disconnected")

    async def simulate_disconnect(self) -> None:
        """Drop the link as an unplugged cable would (conformance hook)."""
        self._link.simulate_drop()

    # -- manifest -----------------------------------------------------------

    def _available(self, gesture: Gesture) -> bool:
        return bool(gesture.joints) and gesture.joints <= frozenset(self._channels)

    def _refresh_manifest(self) -> None:
        capabilities = {"face.leds"}
        for name in self._channels:
            capability = CHANNEL_CAPABILITIES.get(name)
            if capability is not None:
                capabilities.add(capability)
        implements: dict[str, dict[str, Any]] = {}
        for gesture in self._gestures.values():
            if not self._available(gesture):
                continue
            capabilities.update(c for c in gesture.requires if is_capability(c))
            implements[gesture.name] = {
                "primitive": "gesture",
                "arg": gesture.name,
                "est_ms": gesture.duration_ms,
            }
        self.manifest = BodyManifest(
            name=_BASE_MANIFEST.name,
            kind_of_body=_BASE_MANIFEST.kind_of_body,
            capabilities=tuple(sorted(capabilities)),
            implements=implements,
            limits={
                **_BASE_MANIFEST.limits,
                "joints": {
                    name: [spec.min_deg, spec.max_deg] for name, spec in self._channels.items()
                },
            },
            safety=dict(_BASE_MANIFEST.safety),
        )

    # -- primitives ---------------------------------------------------------

    async def gesture(self, name: str, params: dict[str, Any], *, timeout_s: float) -> BehaviorResult:
        gesture = self._gestures.get(name)
        if gesture is None or not self._available(gesture):
            return BehaviorResult.unsupported(f"{name} needs joints this bust does not have")
        if not self._link.connected:
            return BehaviorResult.error("link down")

        await self._cancel_action()
        if gesture.duration_ms > SHORT_GESTURE_MAX_MS:
            action_id = new_id()
            self._action_task = asyncio.create_task(
                self._run_action(gesture, action_id), name=f"inmoov-{name}"
            )
            return BehaviorResult.started(action_id, gesture.duration_ms / 1000.0)

        try:
            finished, _ = await run_with_timeout(self._runner.run(gesture), timeout_s)
        except GestureError as exc:
            return BehaviorResult.error(str(exc))
        if not finished:
            await self._runner.interrupt()
            return BehaviorResult.timeout(f"{name} exceeded {timeout_s}s")
        return BehaviorResult.ok()

    async def _run_action(self, gesture: Gesture, action_id: str) -> None:
        status, reason = "ok", None
        try:
            await self._runner.run(gesture)
        except asyncio.CancelledError:
            # The core still owes the model an outcome for this action_id.
            self._reply(action_id, gesture.name, "canceled", "interrupted")
            raise
        except GestureError as exc:
            status, reason = "error", str(exc)
            LOG.warning("inmoov: %s failed: %s", gesture.name, exc)
        except Exception as exc:
            status, reason = "error", str(exc)
            LOG.exception("inmoov: %s crashed", gesture.name)
            self._faults.record("gesture", str(exc))
        self._reply(action_id, gesture.name, status, reason)

    def _reply(self, action_id: str, name: str, status: str, reason: str | None) -> None:
        self._publish(
            TOPICS.BODY_REPLY,
            {"action_id": action_id, "name": name, "status": status, "reason": reason},
        )

    async def move(self, vx: float, vy: float, wz: float, duration_s: float) -> BehaviorResult:
        return BehaviorResult.unsupported("an InMoov bust does not move around")

    async def look_at(self, target: GazeTarget) -> None:
        self._gaze.set_target(target)

    async def set_face(self, state: FaceState) -> None:
        if not self._link.connected:
            return
        await self._face.render(state)
        if self._jaw is not None:
            await self._jaw.update(state.lip)

    async def stop_all(self, reason: str) -> None:
        # `!` before anything else: the latch it sets is also what tells the
        # gesture runner not to try to freeze a servo the board just froze.
        sent, _ = await run_with_timeout(self._link.send_priority("!"), STOP_ALL_TIMEOUT_S)
        if not sent:
            LOG.error("inmoov: the stop line did not go out within %.1fs", STOP_ALL_TIMEOUT_S)
        await self._cancel_action()
        LOG.warning("inmoov: stop_all (%s)", reason)

    def audio_source(self) -> AudioSource | None:
        return None

    def audio_sink(self) -> AudioSink | None:
        return None

    # -- internals ----------------------------------------------------------

    async def _cancel_action(self) -> None:
        task = self._action_task
        self._action_task = None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await self._runner.interrupt()

    def _publish(self, topic: str, data: dict[str, Any], *, kind: str = "reply") -> None:
        ctx = self._ctx
        if ctx is None:
            return
        if ctx.bus is not None:
            result = ctx.bus.publish(topic, data, kind=kind)
        elif callable(ctx.publish):
            result = ctx.publish(topic, data, kind=kind)
        else:
            return
        if asyncio.iscoroutine(result):
            # Strong reference: a task only the loop holds can be collected
            # mid-flight and the reply never leaves (review, major 15).
            task = asyncio.ensure_future(result)
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)


def _extra_dirs(config: dict[str, Any]) -> tuple[Path, ...]:
    directory = config.get("gestures_dir")
    return (Path(directory),) if directory else ()
