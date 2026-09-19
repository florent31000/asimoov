"""Memory contract: per-person facts, episodes, and the journal (SQLite + FTS5).

See plan.md section 4.3. Face/voice embeddings never leave the store:
`recall` and every read method here return text or metadata only, which is
also why this module never imports numpy -- the real
`asimoov.core.memory.sqlite_store.SqliteMemoryStore` (WS1) does the vector
math internally and returns plain Python values across this boundary.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Person:
    """A remembered person."""

    id: str
    name: str | None = None
    created_at: float = field(default_factory=time.time)
    last_seen_at: float | None = None
    relationship: str | None = None
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "relationship": self.relationship,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Person:
        return cls(
            id=payload["id"],
            name=payload.get("name"),
            created_at=payload.get("created_at", time.time()),
            last_seen_at=payload.get("last_seen_at"),
            relationship=payload.get("relationship"),
            notes=payload.get("notes"),
        )


@dataclass(frozen=True)
class Fact:
    """One remembered fact about a person."""

    person_id: str
    text: str
    confidence: float = 1.0
    source: str = "conversation"
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "person_id": self.person_id,
            "text": self.text,
            "confidence": self.confidence,
            "source": self.source,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Fact:
        return cls(
            person_id=payload["person_id"],
            text=payload["text"],
            confidence=payload.get("confidence", 1.0),
            source=payload.get("source", "conversation"),
            ts=payload.get("ts", time.time()),
        )


@dataclass(frozen=True)
class Episode:
    """A conversation episode, summarized when it ends."""

    id: str
    started_at: float
    ended_at: float | None = None
    participants: tuple[str, ...] = ()
    summary: str | None = None
    mood: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "participants": list(self.participants),
            "summary": self.summary,
            "mood": self.mood,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Episode:
        return cls(
            id=payload["id"],
            started_at=payload["started_at"],
            ended_at=payload.get("ended_at"),
            participants=tuple(payload.get("participants", [])),
            summary=payload.get("summary"),
            mood=payload.get("mood"),
        )


@dataclass(frozen=True)
class JournalEntry:
    """A free-text line in the robot's journal (self-narrated events)."""

    ts: float
    text: str
    kind: str = "note"

    def to_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "text": self.text, "kind": self.kind}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> JournalEntry:
        return cls(ts=payload["ts"], text=payload["text"], kind=payload.get("kind", "note"))


class MemoryStore(ABC):
    """Per-person memory, backed by SQLite + FTS5 in the reference implementation.

    Every method that could expose a raw embedding or a numeric aggregate
    instead returns only text, a person id, or a similarity score: this is
    the boundary enforced so nothing but text ever reaches an LLM prompt.
    """

    @abstractmethod
    async def get_person(self, person_id: str) -> Person | None:
        """Return the person by id, or None if unknown."""

    @abstractmethod
    async def find_person_by_name(self, name: str) -> Person | None:
        """Return the first person matching ``name`` (case-insensitive), or None."""

    @abstractmethod
    async def upsert_person(self, person: Person) -> Person:
        """Insert or update a person by id. Returns the stored value."""

    @abstractmethod
    async def add_face_embedding(
        self, person_id: str, model: str, vec: Any, quality: float
    ) -> None:
        """Store a face embedding for ``person_id``.

        ``vec`` is an opaque fixed-length float vector (e.g. a numpy
        array); this contract does not depend on numpy, so implementations
        are free to accept anything array-like they can persist as BLOB.
        """

    @abstractmethod
    async def match_face(self, vec: Any) -> tuple[str, float] | None:
        """Return ``(person_id, score)`` for the closest gallery match.

        Matching runs against an in-RAM gallery (cosine similarity in the
        reference implementation); returns None below the store's unknown
        threshold. Never returns or logs the embedding itself.
        """

    @abstractmethod
    async def add_fact(self, fact: Fact) -> None:
        """Store a fact about a person."""

    @abstractmethod
    async def recall(self, query: str, k: int = 5) -> list[str]:
        """Full-text search over facts, episode summaries, and journal entries.

        Returns up to ``k`` plain-text snippets, ranked by relevance. Never
        returns embeddings, raw rows, or any numeric aggregate: this is the
        method the mind's prompt injector calls, and its output goes
        straight into an LLM-visible string.
        """

    @abstractmethod
    async def start_episode(self, participants: tuple[str, ...]) -> Episode:
        """Begin a new episode with the given participant person ids."""

    @abstractmethod
    async def end_episode(self, episode_id: str, summary: str, mood: str | None = None) -> None:
        """Close an episode with a short summary and optional mood tag.

        Raises:
            KeyError: if ``episode_id`` does not refer to an open episode.
        """

    @abstractmethod
    async def journal(self, text: str, kind: str = "note") -> None:
        """Append a free-text line to the journal, timestamped now."""

    async def delete_person(self, person_id: str) -> bool:
        """Optional (v1.3). Forget a person entirely. False if unknown.

        Removes the identity, their facts, the episodes they took part in
        and their face embeddings, in one transaction. Not abstract, so a
        v1.2 store still instantiates; the default refuses rather than
        pretending it forgot (the ``forget_person`` tool reports the
        refusal to the user).
        """
        raise NotImplementedError(
            f"{type(self).__name__} cannot delete a person: 'forget me' is unavailable"
        )

    async def reload_gallery(self) -> int:
        """Optional (v1.3). Rebuild the in-RAM face gallery, return its size.

        Called after `delete_person` so a forgotten face stops being
        recognized without restarting the process. Stores with no gallery
        return 0.
        """
        return 0
