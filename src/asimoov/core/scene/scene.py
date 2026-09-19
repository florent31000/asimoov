"""`SocialScene`: an immutable snapshot of the social situation, plus its tracker.

The scene is rebuilt from percepts by `SceneTracker` and published on
``scene.state`` as a latest-value envelope. Nothing mutates a snapshot:
consumers (mind, prompt builder, face) hold one and compare.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any

from asimoov.contracts.envelope import Envelope
from asimoov.contracts.percepts import Bearing
from asimoov.contracts.vocab import TOPICS
from asimoov.core.scene.attention import AttentionPolicy, attribute_speaker

FORGET_AFTER_S = 8.0


@dataclass(frozen=True)
class Presence:
    """One person currently (or very recently) perceived."""

    track_id: str
    bearing: Bearing
    distance_class: str = "medium"
    confidence: float = 0.0
    person_id: str | None = None
    name: str | None = None
    first_seen: float = 0.0
    last_seen: float = 0.0
    speaking: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "bearing": {"az": self.bearing.az, "el": self.bearing.el},
            "distance_class": self.distance_class,
            "confidence": self.confidence,
            "person_id": self.person_id,
            "name": self.name,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "speaking": self.speaking,
        }


@dataclass(frozen=True)
class SocialScene:
    """Immutable snapshot published on ``scene.state``."""

    ts: float = field(default_factory=time.time)
    people: tuple[Presence, ...] = ()
    attention_track_id: str | None = None
    speaker_track_id: str | None = None
    speech_active: bool = False
    last_speech_ended_at: float | None = None

    def person(self, track_id: str | None) -> Presence | None:
        if track_id is None:
            return None
        return next((person for person in self.people if person.track_id == track_id), None)

    def attention(self) -> Presence | None:
        return self.person(self.attention_track_id)

    def speaker(self) -> Presence | None:
        return self.person(self.speaker_track_id)

    def known_people(self) -> tuple[Presence, ...]:
        return tuple(person for person in self.people if person.person_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "people": [person.to_dict() for person in self.people],
            "attention_track_id": self.attention_track_id,
            "speaker_track_id": self.speaker_track_id,
            "speech_active": self.speech_active,
            "last_speech_ended_at": self.last_speech_ended_at,
        }


class SceneTracker:
    """Applies percepts and hands out `SocialScene` snapshots."""

    def __init__(
        self,
        *,
        attention: AttentionPolicy | None = None,
        forget_after_s: float = FORGET_AFTER_S,
    ) -> None:
        self.attention_policy = attention or AttentionPolicy()
        self.forget_after_s = forget_after_s
        self._people: dict[str, Presence] = {}
        self._attention: str | None = None
        self._attention_since: float = 0.0
        self._speaker: str | None = None
        self._speech_active = False
        self._last_speech_ended_at: float | None = None

    def snapshot(self, now: float | None = None) -> SocialScene:
        now = time.time() if now is None else now
        people = tuple(sorted(self._people.values(), key=lambda person: person.bearing.az))
        return SocialScene(
            ts=now,
            people=people,
            attention_track_id=self._attention,
            speaker_track_id=self._speaker,
            speech_active=self._speech_active,
            last_speech_ended_at=self._last_speech_ended_at,
        )

    def apply_envelope(self, envelope: Envelope, *, now: float | None = None) -> bool:
        """Apply a ``percept.*`` envelope. Returns True if the scene changed."""
        if not envelope.topic.startswith(TOPICS.PERCEPT_PREFIX):
            return False
        percept_type = envelope.topic[len(TOPICS.PERCEPT_PREFIX) :] or envelope.data.get("type", "")
        return self.apply(percept_type, envelope.data, now=now)

    def apply(self, percept_type: str, data: dict[str, Any], *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        handler = getattr(self, f"_on_{percept_type}", None)
        if handler is None:
            return False
        changed = handler(data, now)
        if changed:
            self._recompute(now)
        return bool(changed)

    def tick(self, now: float | None = None) -> SocialScene:
        """Forget stale presences and recompute attention. Returns the snapshot."""
        now = time.time() if now is None else now
        stale = [
            track_id
            for track_id, person in self._people.items()
            if now - person.last_seen > self.forget_after_s
        ]
        for track_id in stale:
            del self._people[track_id]
        self._recompute(now)
        return self.snapshot(now)

    # -- percept handlers -------------------------------------------------

    def _on_person_seen(self, data: dict[str, Any], now: float) -> bool:
        track_id = data["track_id"]
        raw_bearing = data.get("bearing") or {}
        bearing = Bearing(az=raw_bearing.get("az", 0.0), el=raw_bearing.get("el", 0.0))
        existing = self._people.get(track_id)
        person = Presence(
            track_id=track_id,
            bearing=bearing,
            distance_class=data.get("distance_class", "medium"),
            confidence=data.get("confidence", 0.0),
            person_id=data.get("person_id") or (existing.person_id if existing else None),
            name=data.get("name") or (existing.name if existing else None),
            first_seen=existing.first_seen if existing else now,
            last_seen=now,
            speaking=existing.speaking if existing else False,
        )
        self._people[track_id] = person
        return True

    def _on_person_lost(self, data: dict[str, Any], now: float) -> bool:
        track_id = data["track_id"]
        if self._people.pop(track_id, None) is None:
            return False
        if self._attention == track_id:
            self._attention = None
        if self._speaker == track_id:
            self._speaker = None
        return True

    def _on_speech_started(self, data: dict[str, Any], now: float) -> bool:
        raw_direction = data.get("direction")
        direction = (
            Bearing(az=raw_direction.get("az", 0.0), el=raw_direction.get("el", 0.0))
            if isinstance(raw_direction, dict)
            else None
        )
        self._speech_active = True
        self._speaker = attribute_speaker(
            self._people.values(), direction=direction, current_attention=self._attention
        )
        self._set_speaking(self._speaker)
        return True

    def _on_speech_ended(self, data: dict[str, Any], now: float) -> bool:
        self._speech_active = False
        self._last_speech_ended_at = now
        self._set_speaking(None)
        return True

    def _on_utterance(self, data: dict[str, Any], now: float) -> bool:
        track_id = data.get("speaker_track_id")
        if track_id and track_id in self._people:
            self._speaker = track_id
            self._set_speaking(track_id)
            return True
        return False

    # -- internals --------------------------------------------------------

    def _set_speaking(self, track_id: str | None) -> None:
        for key, person in self._people.items():
            speaking = key == track_id
            if person.speaking != speaking:
                self._people[key] = replace(person, speaking=speaking)

    def _recompute(self, now: float) -> None:
        chosen = self.attention_policy.choose(
            self._people.values(),
            current=self._attention,
            speaker_track_id=self._speaker,
            now=now,
            chosen_at=self._attention_since,
        )
        if chosen != self._attention:
            self._attention = chosen
            self._attention_since = now

    def bind_person(self, track_id: str, person_id: str, name: str | None) -> bool:
        """Attach an identity to a live track (``remember_person`` does this)."""
        person = self._people.get(track_id)
        if person is None:
            return False
        self._people[track_id] = replace(person, person_id=person_id, name=name or person.name)
        return True
