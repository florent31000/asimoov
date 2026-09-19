"""Bus envelope (envelope.v1): the one message shape used locally and remotely.

See ``schemas/envelope.v1.json`` for the JSON Schema and
``examples/envelope.person_seen.json`` for a worked example.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from asimoov.contracts.vocab import ENVELOPE_KINDS

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_crockford(value: int, length: int) -> str:
    chars = ["0"] * length
    for i in range(length - 1, -1, -1):
        value, rem = divmod(value, 32)
        chars[i] = _CROCKFORD_ALPHABET[rem]
    return "".join(chars)


def new_id() -> str:
    """Generate a ULID-like, lexicographically sortable id (stdlib only).

    Layout: 48-bit millisecond timestamp (10 Crockford-base32 chars) followed
    by 80 bits of randomness (16 chars) -- 26 characters total, matching the
    ULID spec without pulling in a third-party dependency.
    """
    ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    randomness = int.from_bytes(os.urandom(10), "big")
    return _encode_crockford(ts_ms, 10) + _encode_crockford(randomness, 16)


@dataclass(frozen=True)
class Envelope:
    """One bus message, local (in-process) or remote (hub WebSocket).

    Attributes:
        v: Envelope schema version. Always ``1`` for contracts v1.
        kind: One of ``vocab.ENVELOPE_KINDS``
            (percept | state | cmd | reply | frame | log | metric).
        topic: Dotted topic, e.g. ``percept.person_seen`` or ``body.cmd``.
        id: Unique id, ULID-like and time-sortable. Generated automatically.
        ts: Unix timestamp (seconds, float) of creation.
        src: Producer module id, e.g. ``perception.face_id``.
        corr: Correlation id linking a ``reply`` to its ``cmd``, or None.
        data: JSON-serializable payload. Never contains raw camera frames;
            those travel as binary ``frame.*`` messages outside the envelope.

    Raises:
        ValueError: if ``kind`` is not one of ``vocab.ENVELOPE_KINDS``, or if
            ``topic`` is empty, at construction time (``__post_init__``).
    """

    kind: str
    topic: str
    src: str
    data: dict[str, Any] = field(default_factory=dict)
    corr: str | None = None
    v: int = 1
    id: str = field(default_factory=new_id)
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.kind not in ENVELOPE_KINDS:
            raise ValueError(f"invalid envelope kind: {self.kind!r} (expected one of {ENVELOPE_KINDS})")
        if not self.topic:
            raise ValueError("envelope topic must not be empty")

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict matching ``schemas/envelope.v1.json``."""
        return {
            "v": self.v,
            "kind": self.kind,
            "topic": self.topic,
            "id": self.id,
            "ts": self.ts,
            "src": self.src,
            "corr": self.corr,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Envelope:
        """Deserialize from a dict matching ``schemas/envelope.v1.json``.

        Raises:
            KeyError: if a required field (kind, topic, src) is missing.
            ValueError: if ``kind`` is not in ``vocab.ENVELOPE_KINDS``.
        """
        return cls(
            kind=payload["kind"],
            topic=payload["topic"],
            src=payload["src"],
            data=payload.get("data", {}),
            corr=payload.get("corr"),
            v=payload.get("v", 1),
            id=payload.get("id") or new_id(),
            ts=payload.get("ts", time.time()),
        )
