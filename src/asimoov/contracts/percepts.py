"""Percept payloads (percept.v1): the closed vocabulary of what a robot senses.

Each dataclass below is the ``data`` field of an ``Envelope`` whose ``kind``
is ``"percept"`` and whose ``topic`` is ``percept.<type>`` (see
``vocab.PERCEPT_TYPES``). Apps may publish their own percepts under
``x.<app>.<name>``; those are free-form dicts, not modeled here.

See ``schemas/percept.v1.json`` (oneOf per type) and
``examples/percept.person_seen.json``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, ClassVar

from asimoov.contracts.vocab import DISTANCE_CLASSES

TOUCH_LOCATIONS: tuple[str, ...] = ("head", "back", "hand_l", "hand_r", "screen")


@dataclass(frozen=True)
class Bearing:
    """Angular position relative to the body's forward axis, in degrees.

    Sign convention (frozen, see ``docs/contracts.md`` "Units and
    conventions"): ``az`` positive = to the robot's own left (i.e.
    counter-clockwise seen from above, ROS REP-103); ``el`` positive = up.
    A person standing to the robot's left has ``az > 0``.
    """

    az: float
    el: float = 0.0


@dataclass(frozen=True)
class PersonSeen:
    """A tracked person is currently visible.

    Raises:
        ValueError: if ``distance_class`` is not in ``vocab.DISTANCE_CLASSES``.
    """

    PERCEPT_TYPE: ClassVar[str] = "person_seen"

    track_id: str
    confidence: float
    bearing: Bearing
    distance_class: str
    bbox_norm: tuple[float, float, float, float]
    face_quality: float
    person_id: str | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        if self.distance_class not in DISTANCE_CLASSES:
            raise ValueError(f"invalid distance_class: {self.distance_class!r}")

    def to_dict(self) -> dict[str, Any]:
        payload = {"type": self.PERCEPT_TYPE, **asdict(self)}
        # JSON arrays, not tuples: the dict must validate against
        # percept.v1.json before it is serialized, not only after.
        payload["bbox_norm"] = list(self.bbox_norm)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PersonSeen:
        bearing = payload["bearing"]
        if isinstance(bearing, dict):
            bearing = Bearing(**bearing)
        return cls(
            track_id=payload["track_id"],
            confidence=payload["confidence"],
            bearing=bearing,
            distance_class=payload["distance_class"],
            bbox_norm=tuple(payload["bbox_norm"]),
            face_quality=payload["face_quality"],
            person_id=payload.get("person_id"),
            name=payload.get("name"),
        )


@dataclass(frozen=True)
class PersonLost:
    """A previously tracked person is no longer visible."""

    PERCEPT_TYPE: ClassVar[str] = "person_lost"

    track_id: str
    last_bearing: Bearing
    person_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.PERCEPT_TYPE, **asdict(self)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PersonLost:
        last_bearing = payload["last_bearing"]
        if isinstance(last_bearing, dict):
            last_bearing = Bearing(**last_bearing)
        return cls(
            track_id=payload["track_id"],
            last_bearing=last_bearing,
            person_id=payload.get("person_id"),
        )


@dataclass(frozen=True)
class SpeechStarted:
    """Voice activity started (server VAD or local VAD module)."""

    PERCEPT_TYPE: ClassVar[str] = "speech_started"

    source: str
    direction: Bearing | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.PERCEPT_TYPE, **asdict(self)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SpeechStarted:
        direction = payload.get("direction")
        if isinstance(direction, dict):
            direction = Bearing(**direction)
        return cls(source=payload["source"], direction=direction)


@dataclass(frozen=True)
class SpeechEnded:
    """Voice activity ended (server VAD or local VAD module)."""

    PERCEPT_TYPE: ClassVar[str] = "speech_ended"

    source: str
    direction: Bearing | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.PERCEPT_TYPE, **asdict(self)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SpeechEnded:
        direction = payload.get("direction")
        if isinstance(direction, dict):
            direction = Bearing(**direction)
        return cls(source=payload["source"], direction=direction)


@dataclass(frozen=True)
class Utterance:
    """A transcribed chunk of speech, produced by the voice provider."""

    PERCEPT_TYPE: ClassVar[str] = "utterance"

    text: str
    lang: str
    final: bool
    speaker_track_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.PERCEPT_TYPE, **asdict(self)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Utterance:
        return cls(
            text=payload["text"],
            lang=payload["lang"],
            final=payload["final"],
            speaker_track_id=payload.get("speaker_track_id"),
        )


@dataclass(frozen=True)
class Touched:
    """A touch sensor (or the face page's tap handler) fired.

    Raises:
        ValueError: if ``where`` is not in ``TOUCH_LOCATIONS``.
    """

    PERCEPT_TYPE: ClassVar[str] = "touched"

    where: str
    intensity: float = 1.0

    def __post_init__(self) -> None:
        if self.where not in TOUCH_LOCATIONS:
            raise ValueError(f"invalid touch location: {self.where!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.PERCEPT_TYPE, **asdict(self)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Touched:
        return cls(where=payload["where"], intensity=payload.get("intensity", 1.0))


@dataclass(frozen=True)
class Battery:
    """Body battery level."""

    PERCEPT_TYPE: ClassVar[str] = "battery"

    level: float
    charging: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.PERCEPT_TYPE, **asdict(self)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Battery:
        return cls(level=payload["level"], charging=payload.get("charging", False))


@dataclass(frozen=True)
class BodyState:
    """Coarse body posture/motion state."""

    PERCEPT_TYPE: ClassVar[str] = "body_state"

    posture: str
    moving: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.PERCEPT_TYPE, **asdict(self)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> BodyState:
        return cls(posture=payload["posture"], moving=payload.get("moving", False))


@dataclass(frozen=True)
class SoundEvent:
    """A non-speech sound was classified (optional, perception extra)."""

    PERCEPT_TYPE: ClassVar[str] = "sound_event"

    label: str
    level: float

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.PERCEPT_TYPE, **asdict(self)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SoundEvent:
        return cls(label=payload["label"], level=payload["level"])


@dataclass(frozen=True)
class AppEvent:
    """Free-form event published by an app, under topic ``x.<app>.<name>``."""

    PERCEPT_TYPE: ClassVar[str] = "app_event"

    name: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.PERCEPT_TYPE, "name": self.name, "payload": self.payload}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AppEvent:
        return cls(name=payload["name"], payload=payload.get("payload", {}))


PERCEPT_CLASSES: dict[str, type] = {
    cls.PERCEPT_TYPE: cls
    for cls in (
        PersonSeen,
        PersonLost,
        SpeechStarted,
        SpeechEnded,
        Utterance,
        Touched,
        Battery,
        BodyState,
        SoundEvent,
        AppEvent,
    )
}


def decode_percept(percept_type: str, data: dict[str, Any]) -> Any:
    """Decode a percept payload dict into its typed dataclass.

    Raises:
        KeyError: if ``percept_type`` is not in ``vocab.PERCEPT_TYPES``.
    """
    return PERCEPT_CLASSES[percept_type].from_dict(data)
