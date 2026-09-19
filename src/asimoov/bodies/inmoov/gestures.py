"""Keyframe gestures: YAML in, ``M <ch>:<deg>,... T<dt>`` sequences out."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from asimoov.bodies.inmoov.link import ChannelSpec, Link

LOG = logging.getLogger(__name__)


class GestureError(RuntimeError):
    """A gesture file is malformed, or the firmware refused a keyframe."""


@dataclass(frozen=True)
class Keyframe:
    """One pose to reach at ``t_ms`` after the start of the gesture."""

    t_ms: int
    pose: dict[str, int]


@dataclass(frozen=True)
class Gesture:
    """A named sequence of keyframes, loaded from ``gestures/<name>.yaml``."""

    name: str
    requires: tuple[str, ...]
    keyframes: tuple[Keyframe, ...]

    @property
    def duration_ms(self) -> int:
        return self.keyframes[-1].t_ms if self.keyframes else 0

    @property
    def joints(self) -> frozenset[str]:
        return frozenset(joint for frame in self.keyframes for joint in frame.pose)


def load_gesture(path: Path) -> Gesture:
    """Load one gesture file.

    Raises:
        GestureError: if the file has no name, no keyframe, or a keyframe
            without a ``t``/``pose``, or if timestamps are not increasing.
    """
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    name = payload.get("name")
    if not name:
        raise GestureError(f"{path}: missing 'name'")
    raw_frames = payload.get("keyframes") or []
    if not raw_frames:
        raise GestureError(f"{path}: missing 'keyframes'")

    frames: list[Keyframe] = []
    previous = -1
    for raw in raw_frames:
        if "t" not in raw or not raw.get("pose"):
            raise GestureError(f"{path}: every keyframe needs 't' and a non-empty 'pose'")
        t_ms = int(raw["t"])
        if t_ms <= previous:
            raise GestureError(f"{path}: keyframe times must increase, got {t_ms} after {previous}")
        previous = t_ms
        frames.append(Keyframe(t_ms=t_ms, pose={str(k): int(v) for k, v in raw["pose"].items()}))

    return Gesture(
        name=str(name),
        requires=tuple(payload.get("requires") or ()),
        keyframes=tuple(frames),
    )


def load_gestures(*directories: Path) -> dict[str, Gesture]:
    """Load every ``*.yaml`` of each directory, later ones overriding earlier."""
    gestures: dict[str, Gesture] = {}
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")):
            gesture = load_gesture(path)
            gestures[gesture.name] = gesture
    return gestures


def to_commands(
    gesture: Gesture, channels: Mapping[str, ChannelSpec]
) -> list[tuple[str, int]]:
    """Turn a gesture into ``(command, dt_ms)`` pairs, one per keyframe.

    Angles are clamped a second time here with the limits read from the
    firmware's ``V``: the firmware clamps too, but a gesture written against
    another bust must not silently ask for an impossible angle.

    Raises:
        GestureError: if a keyframe names a joint this body does not have.
    """
    commands: list[tuple[str, int]] = []
    previous_t = 0
    for frame in gesture.keyframes:
        dt_ms = max(0, frame.t_ms - previous_t)
        previous_t = frame.t_ms
        parts = []
        for joint, deg in frame.pose.items():
            spec = channels.get(joint)
            if spec is None:
                raise GestureError(f"{gesture.name}: no channel named {joint!r} on this body")
            parts.append(f"{spec.id}:{spec.clamp(deg)}")
        commands.append((f"M {','.join(parts)} T{dt_ms}", dt_ms))
    return commands


class GestureRunner:
    """Plays gestures on a `Link`, one keyframe at a time."""

    def __init__(self, link: Link, channels: Mapping[str, ChannelSpec]) -> None:
        self._link = link
        self._channels = channels
        self._active: frozenset[int] = frozenset()

    async def run(self, gesture: Gesture) -> None:
        """Attach the joints, then send each keyframe and wait out its duration.

        Raises:
            GestureError: if the firmware refuses a command.
            asyncio.CancelledError: if the caller interrupts the gesture;
                the caller then calls `interrupt` to freeze the servos.
        """
        commands = to_commands(gesture, self._channels)
        self._active = frozenset(self._channels[joint].id for joint in gesture.joints)
        for joint in sorted(gesture.joints):
            spec = self._channels[joint]
            reply = await self._link.send(f"E {spec.id}")
            if not reply.ok:
                raise GestureError(f"{gesture.name}: E {spec.id} refused ({reply.error})")
        for command, dt_ms in commands:
            reply = await self._link.send(command)
            if not reply.ok:
                raise GestureError(f"{gesture.name}: {command} refused ({reply.error})")
            await asyncio.sleep(dt_ms / 1000.0)
        self._active = frozenset()

    async def interrupt(self) -> None:
        """Freeze the interrupted gesture's channels where they stand.

        Not ``!``: the E-STOP detaches every servo 300 ms later and the arm
        falls. A ``T0`` move on each moving channel stops it and leaves it
        attached, holding its own weight.
        """
        active, self._active = self._active, frozenset()
        if not active or self._link.estopped:
            return
        state = await self._link.send("?")
        if not state.ok:
            LOG.warning("inmoov: cannot freeze, ? refused (%s)", state.error)
            return
        parts = [
            f"{channel_id}:{degrees}"
            for channel_id, degrees in _moving(state.lines)
            if channel_id in active
        ]
        if not parts:
            return
        reply = await self._link.send(f"M {','.join(parts)} T0")
        if not reply.ok:
            LOG.warning("inmoov: freeze refused (%s)", reply.error)


def _moving(lines: tuple[str, ...]) -> list[tuple[int, str]]:
    """``ST <id> <name> <deg> <min> <max> <attached> <moving>`` of moving channels."""
    moving = []
    for line in lines:
        fields = line.split()
        if len(fields) == 8 and fields[0] == "ST" and fields[7] == "1":
            moving.append((int(fields[1]), fields[3]))
    return moving
