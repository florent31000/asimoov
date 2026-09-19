"""Replaying a recorded percept stream through a live runtime, with assertions.

A replay file is JSONL: envelope.v1 objects in recorded order, optionally
interleaved with assertion lines (``{"assert": "expect", ...}``) that check
what the core did in response. `tools/record_percepts.py` writes the
envelope lines; assertions are added by hand.

While a replay runs, the `Replayer` also *stands in for* the perception
process: it published the recorded ``percept.person_seen`` lines, so it
also answers the ``perception.face_id.*`` commands the core sends back
(enrolling a face, reloading the gallery). Without that the core would
correctly report "no perception" and a recorded identity scenario could
never be replayed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from asimoov.contracts.envelope import Envelope

log = logging.getLogger(__name__)

POLL_S = 0.005
DEFAULT_WITHIN_S = 3.0
DEFAULT_MAX_GAP_S = 5.0
FACE_ID_PREFIX = "perception.face_id."
REPLAY_ENROLL_SAMPLES = 5


class ReplayError(ValueError):
    """Raised when a replay file cannot be parsed. The message names the line."""


class ReplayAssertionError(AssertionError):
    """Raised when a replay assertion is not satisfied in time."""


@dataclass
class ReplayResult:
    """What a replay did: envelopes published, assertions checked, wall time."""

    envelopes: int = 0
    assertions: int = 0
    duration_s: float = 0.0
    observed: list[str] = field(default_factory=list)


def first_timestamp(path: str | Path) -> float | None:
    """The ``ts`` of the first envelope in a replay file, or None if there is none."""
    path = Path(path)
    if not path.is_file():
        raise ReplayError(f"{path}: no such replay file")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        payload = json.loads(line)
        if "assert" not in payload:
            return float(payload.get("ts", 0.0))
    return None


class Replayer:
    """Publishes a recorded stream onto a bus and checks the assertions."""

    def __init__(
        self,
        bus,
        *,
        speed: float = 1.0,
        clock=None,
        max_gap_s: float = DEFAULT_MAX_GAP_S,
    ) -> None:
        if speed <= 0:
            raise ValueError(f"speed must be positive, got {speed!r}")
        self.bus = bus
        self.speed = speed
        self.clock = clock
        self.max_gap_s = max_gap_s
        self._log: list[Envelope] = []
        self._cursor = 0
        self._subscription = None
        self._face_id_subscription = None
        self.face_id_commands: list[Envelope] = []

    async def run(self, path: str | Path) -> ReplayResult:
        """Replay ``path``, returning counts.

        Raises:
            ReplayError: on a malformed line.
            ReplayAssertionError: on the first assertion that does not hold.
        """
        lines = self._parse(Path(path))
        result = ReplayResult()
        started = time.monotonic()
        self._subscription = self.bus.subscribe("*", self._record)
        self._face_id_subscription = self.bus.subscribe(
            FACE_ID_PREFIX + "*", self._answer_face_id
        )
        try:
            previous_ts: float | None = None
            for number, payload in lines:
                if "assert" in payload:
                    await self._check(number, payload)
                    result.assertions += 1
                    continue
                ts = float(payload.get("ts", previous_ts or 0.0))
                if previous_ts is not None and ts > previous_ts:
                    await self._wait(ts - previous_ts, ts)
                previous_ts = ts
                try:
                    envelope = Envelope.from_dict(payload)
                except (KeyError, ValueError) as exc:
                    raise ReplayError(f"{path}:{number}: invalid envelope ({exc})") from exc
                await self.bus.publish_envelope(envelope)
                result.envelopes += 1
        finally:
            for subscription in (self._subscription, self._face_id_subscription):
                if subscription is not None:
                    subscription.unsubscribe()
            self._subscription = None
            self._face_id_subscription = None
        result.duration_s = time.monotonic() - started
        result.observed = [envelope.topic for envelope in self._log]
        return result

    # -- internals --------------------------------------------------------

    async def _answer_face_id(self, envelope: Envelope) -> None:
        """Answer a ``perception.face_id.*`` command as the real module would.

        The replay is the perception process for the duration of the run:
        the faces in the recording are the ones it "sees", so enrolling one
        succeeds and the gallery reloads.
        """
        if envelope.kind != "cmd":
            return
        self.face_id_commands.append(envelope)
        command = envelope.topic[len(FACE_ID_PREFIX) :]
        data: dict[str, Any] = {"ok": True}
        if command == "enroll":
            data["samples"] = REPLAY_ENROLL_SAMPLES
            data["person_id"] = envelope.data.get("person_id")
        await self.bus.publish(envelope.topic, data, kind="reply", corr=envelope.id)

    async def _wait(self, gap_s: float, next_ts: float) -> None:
        """Sleep the gap between two envelopes, skipping long idle stretches.

        A recording sits idle for minutes at a time; sleeping through them at
        ``1/speed`` would make a replay useless as a test. Past
        ``max_gap_s`` of real waiting the mind's clock is moved straight to
        the next envelope, so cooldowns and hysteresis still see the real
        elapsed time.
        """
        real_s = gap_s / self.speed
        if real_s <= self.max_gap_s or not hasattr(self.clock, "skip_to"):
            await asyncio.sleep(real_s)
            return
        await asyncio.sleep(self.max_gap_s)
        self.clock.skip_to(next_ts)

    @staticmethod
    def _parse(path: Path) -> list[tuple[int, dict[str, Any]]]:
        if not path.is_file():
            raise ReplayError(f"{path}: no such replay file")
        lines: list[tuple[int, dict[str, Any]]] = []
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ReplayError(f"{path}:{number}: invalid JSON ({exc})") from exc
            if not isinstance(payload, dict):
                raise ReplayError(f"{path}:{number}: expected a JSON object")
            lines.append((number, payload))
        return lines

    async def _record(self, envelope: Envelope) -> None:
        self._log.append(envelope)

    async def _check(self, number: int, payload: dict[str, Any]) -> None:
        kind = payload.get("assert")
        if kind != "expect":
            raise ReplayError(f"line {number}: unknown assertion {kind!r} (expected 'expect')")
        topic = payload.get("topic")
        if not isinstance(topic, str):
            raise ReplayError(f"line {number}: 'topic' is required in an expect assertion")
        contains = payload.get("contains", "")
        within_s = float(payload.get("within_s", DEFAULT_WITHIN_S))
        deadline = time.monotonic() + within_s / self.speed

        while True:
            index = self._find(topic, contains)
            if index is not None:
                self._cursor = index + 1
                log.info("replay: %s matched %r", topic, contains)
                return
            if time.monotonic() >= deadline:
                seen = ", ".join(envelope.topic for envelope in self._log[self._cursor :]) or "nothing"
                raise ReplayAssertionError(
                    f"line {number}: expected {topic!r} containing {contains!r} within "
                    f"{within_s}s; saw: {seen}"
                )
            await asyncio.sleep(POLL_S)

    def _find(self, topic: str, contains: str) -> int | None:
        for index in range(self._cursor, len(self._log)):
            envelope = self._log[index]
            if envelope.topic != topic:
                continue
            # Compact separators so an assertion can match a JSON fragment
            # exactly as it is written in the file ('"status":"ok"').
            payload = json.dumps(envelope.data, ensure_ascii=False, separators=(",", ":"))
            if not contains or contains in payload:
                return index
        return None
