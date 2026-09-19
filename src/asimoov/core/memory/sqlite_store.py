"""`SqliteMemoryStore`: the reference `MemoryStore` (SQLite + FTS5 + numpy cosine).

Face embeddings are written to `face_embeddings` and compared in RAM; no
method here returns a vector, so nothing but text can reach an LLM prompt.
Every SQL call runs in a worker thread: sqlite3 is blocking and the runtime
has a single event loop.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
import unicodedata
from pathlib import Path
from typing import Any

import numpy as np

from asimoov.contracts.envelope import new_id
from asimoov.contracts.memory import Episode, Fact, JournalEntry, MemoryStore, Person

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
UNKNOWN_THRESHOLD = 0.38
PERSON_ID_PREFIX = "person:"

#: ``participants`` is a comma-joined list of person ids; the delimiters are
#: added on both sides so ``person:sam`` never matches ``person:sammy``.
EPISODES_OF_PERSON_SQL = (
    "SELECT id FROM episodes WHERE ',' || participants || ',' LIKE '%,' || ? || ',%'"
)


def person_id_for_name(name: str, *, suffix: int = 1) -> str:
    """Stable person id derived from a name (``Sam`` -> ``person:sam``).

    Deterministic on purpose: a face recognized in a later session is
    reported by perception as the same ``person_id``, and a replay fixture
    can reference a person introduced earlier in the same file. Callers
    resolve collisions by asking for the next ``suffix``.
    """
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-") or "unknown"
    return f"{PERSON_ID_PREFIX}{slug}" if suffix <= 1 else f"{PERSON_ID_PREFIX}{slug}-{suffix}"


def _fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 OR query (no operator injection)."""
    tokens = [token for token in re.findall(r"[\w']+", query, flags=re.UNICODE) if token]
    if not tokens:
        return ""
    return " OR ".join(f'"{token}"' for token in tokens)


class SqliteMemoryStore(MemoryStore):
    """Per-person memory in a single SQLite file."""

    def __init__(self, path: str | Path, *, unknown_threshold: float = UNKNOWN_THRESHOLD) -> None:
        self.path = Path(path)
        self.unknown_threshold = unknown_threshold
        self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._gallery: list[tuple[str, np.ndarray]] = []

    # -- lifecycle --------------------------------------------------------

    async def open(self) -> None:
        """Create the file/schema if needed and load the face gallery into RAM."""
        if self._connection is not None:
            return
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await asyncio.to_thread(
            lambda: sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False)
        )
        connection.row_factory = sqlite3.Row
        self._connection = connection
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        await asyncio.to_thread(connection.executescript, schema)
        await self.reload_gallery()

    async def close(self) -> None:
        if self._connection is not None:
            connection = self._connection
            self._connection = None
            await asyncio.to_thread(connection.close)

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("SqliteMemoryStore.open() must be called before use")
        return self._connection

    async def _run(self, sql: str, params: tuple = (), *, fetch: str | None = None) -> Any:
        connection = self._db()

        def execute() -> Any:
            with connection:
                cursor = connection.execute(sql, params)
                if fetch == "one":
                    return cursor.fetchone()
                if fetch == "all":
                    return cursor.fetchall()
                return cursor.lastrowid

        async with self._lock:
            return await asyncio.to_thread(execute)

    # -- persons ----------------------------------------------------------

    @staticmethod
    def _person(row: sqlite3.Row) -> Person:
        return Person(
            id=row["id"],
            name=row["name"],
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            relationship=row["relationship"],
            notes=row["notes"],
        )

    async def get_person(self, person_id: str) -> Person | None:
        row = await self._run(
            "SELECT * FROM persons WHERE id = ?", (person_id,), fetch="one"
        )
        return self._person(row) if row else None

    async def find_person_by_name(self, name: str) -> Person | None:
        row = await self._run(
            "SELECT * FROM persons WHERE name IS NOT NULL AND lower(name) = lower(?) "
            "ORDER BY created_at LIMIT 1",
            (name,),
            fetch="one",
        )
        return self._person(row) if row else None

    async def upsert_person(self, person: Person) -> Person:
        await self._run(
            "INSERT INTO persons (id, name, created_at, last_seen_at, relationship, notes) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
            "last_seen_at = excluded.last_seen_at, relationship = excluded.relationship, "
            "notes = excluded.notes",
            (
                person.id,
                person.name,
                person.created_at,
                person.last_seen_at,
                person.relationship,
                person.notes,
            ),
        )
        return person

    async def touch_person(self, person_id: str, seen_at: float | None = None) -> None:
        """Record that ``person_id`` was just seen."""
        await self._run(
            "UPDATE persons SET last_seen_at = ? WHERE id = ?",
            (seen_at or time.time(), person_id),
        )

    async def delete_person(self, person_id: str) -> bool:
        """Forget a person entirely ("forget me"). Returns False if unknown.

        One transaction removes the identity, their facts, the episodes they
        took part in, their face embeddings, and every full-text entry that
        referenced any of them, so a partial failure leaves nothing behind.
        """
        connection = self._db()

        def purge() -> bool:
            with connection:
                if connection.execute(
                    "SELECT 1 FROM persons WHERE id = ?", (person_id,)
                ).fetchone() is None:
                    return False
                refs = [person_id] + [
                    row[0]
                    for row in connection.execute(EPISODES_OF_PERSON_SQL, (person_id,))
                ]
                connection.executemany(
                    "DELETE FROM mem_fts WHERE ref = ?", [(ref,) for ref in refs]
                )
                connection.execute(
                    f"DELETE FROM episodes WHERE id IN ({EPISODES_OF_PERSON_SQL})", (person_id,)
                )
                connection.execute(
                    "DELETE FROM face_embeddings WHERE person_id = ?", (person_id,)
                )
                connection.execute("DELETE FROM facts WHERE person_id = ?", (person_id,))
                connection.execute("DELETE FROM persons WHERE id = ?", (person_id,))
            return True

        async with self._lock:
            deleted = await asyncio.to_thread(purge)
        if deleted:
            await self.reload_gallery()
        return deleted

    # -- faces ------------------------------------------------------------

    async def add_face_embedding(
        self, person_id: str, model: str, vec: Any, quality: float
    ) -> None:
        array = np.asarray(vec, dtype=np.float32).reshape(-1)
        await self._run(
            "INSERT INTO face_embeddings (person_id, model, dim, vec, quality, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (person_id, model, int(array.size), array.tobytes(), float(quality), time.time()),
        )
        self._gallery.append((person_id, self._normalize(array)))

    async def match_face(self, vec: Any) -> tuple[str, float] | None:
        if not self._gallery:
            return None
        probe = self._normalize(np.asarray(vec, dtype=np.float32).reshape(-1))
        best_id: str | None = None
        best_score = -1.0
        for person_id, candidate in self._gallery:
            if candidate.size != probe.size:
                continue
            score = float(np.dot(probe, candidate))
            if score > best_score:
                best_id, best_score = person_id, score
        if best_id is None or best_score < self.unknown_threshold:
            return None
        return best_id, best_score

    @staticmethod
    def _normalize(array: np.ndarray) -> np.ndarray:
        norm = float(np.linalg.norm(array))
        return array if norm == 0.0 else array / norm

    async def reload_gallery(self) -> int:
        """Rebuild the in-RAM gallery from disk. Returns the number of identities.

        The perception process calls this through
        ``perception.face_id.reload_gallery`` after a person is forgotten or
        enrolled elsewhere, so a stale gallery cannot keep recognizing them.
        """
        rows = await self._run(
            "SELECT person_id, vec FROM face_embeddings", fetch="all"
        )
        self._gallery = [
            (row["person_id"], self._normalize(np.frombuffer(row["vec"], dtype=np.float32)))
            for row in rows or []
        ]
        return len({person_id for person_id, _ in self._gallery})

    # -- facts, episodes, journal ----------------------------------------

    async def add_fact(self, fact: Fact) -> None:
        await self._run(
            "INSERT INTO facts (person_id, text, confidence, source, ts) VALUES (?, ?, ?, ?, ?)",
            (fact.person_id, fact.text, fact.confidence, fact.source, fact.ts),
        )
        await self._index(fact.text, "fact", fact.person_id)

    async def facts_for(self, person_id: str, limit: int = 10) -> list[str]:
        """Most recent facts about ``person_id``, as plain text."""
        rows = await self._run(
            "SELECT text FROM facts WHERE person_id = ? ORDER BY ts DESC LIMIT ?",
            (person_id, limit),
            fetch="all",
        )
        return [row["text"] for row in rows or []]

    async def recall(self, query: str, k: int = 5) -> list[str]:
        match = _fts_query(query)
        if not match:
            return []
        rows = await self._run(
            "SELECT text FROM mem_fts WHERE mem_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, k),
            fetch="all",
        )
        return [row["text"] for row in rows or []]

    async def start_episode(self, participants: tuple[str, ...]) -> Episode:
        episode = Episode(id=new_id(), started_at=time.time(), participants=participants)
        await self._run(
            "INSERT INTO episodes (id, started_at, participants) VALUES (?, ?, ?)",
            (episode.id, episode.started_at, ",".join(participants)),
        )
        return episode

    async def end_episode(self, episode_id: str, summary: str, mood: str | None = None) -> None:
        row = await self._run(
            "SELECT id, ended_at FROM episodes WHERE id = ?", (episode_id,), fetch="one"
        )
        if row is None or row["ended_at"] is not None:
            raise KeyError(f"no open episode {episode_id!r}")
        await self._run(
            "UPDATE episodes SET ended_at = ?, summary = ?, mood = ? WHERE id = ?",
            (time.time(), summary, mood, episode_id),
        )
        await self._index(summary, "episode", episode_id)

    async def journal(self, text: str, kind: str = "note") -> None:
        entry = JournalEntry(ts=time.time(), text=text, kind=kind)
        await self._run(
            "INSERT INTO journal (ts, text, kind) VALUES (?, ?, ?)",
            (entry.ts, entry.text, entry.kind),
        )
        await self._index(entry.text, f"journal:{kind}", None)

    async def _index(self, text: str, kind: str, ref: str | None) -> None:
        await self._run(
            "INSERT INTO mem_fts (text, kind, ref) VALUES (?, ?, ?)", (text, kind, ref or "")
        )
