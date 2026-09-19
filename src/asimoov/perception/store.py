"""SQLite face store: the default `MemoryStore` the perception process writes to.

This process and WS1's `core/memory/sqlite_store.py` open the same file, so
both create it from the same `core/memory/schema.sql`. Everything outside
faces raises rather than pretending: facts, episodes, recall, and the journal
belong to WS1's store.

Vectors are stored as raw little-endian float32 bytes, `dim` floats long.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any

import numpy as np

from asimoov.contracts.memory import Episode, Fact, MemoryStore, Person
from asimoov.core.config import asimoov_home
from asimoov.core.memory import SCHEMA_PATH
from asimoov.perception.face_id.gallery import UNCERTAIN_THRESHOLD, Gallery
from asimoov.perception.models import EMBEDDING_MODEL_ID


def default_db_path() -> Path:
    return asimoov_home() / "memory.db"


def encode_vector(vec: Any) -> bytes:
    return np.asarray(vec, dtype="<f4").reshape(-1).tobytes()


def decode_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4")


class SqliteFaceStore(MemoryStore):
    """The face-embedding slice of `MemoryStore`, backed by SQLite.

    Raises:
        NotImplementedError: for facts, recall, episodes, and the journal,
            which WS1's store owns.
    """

    def __init__(self, path: Path | str | None = None, *, model: str = EMBEDDING_MODEL_ID) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self.model = model
        self._connection: sqlite3.Connection | None = None
        self._gallery: Gallery | None = None

    def connect(self) -> sqlite3.Connection:
        """Open the database and create the tables if needed (idempotent).

        The schema is `core/memory/schema.sql`, the same file WS1's store
        runs, so whichever process touches the database first leaves it in a
        shape the other one recognizes. It also turns on WAL, so perception
        can write while the core reads.
        """
        if self._connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.path), check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            connection.commit()
            self._connection = connection
        return self._connection

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    # -- faces -------------------------------------------------------------

    def gallery(self) -> Gallery:
        """The in-RAM gallery, loaded once and rebuilt after each write."""
        if self._gallery is None:
            self._gallery = self.load_gallery()
        return self._gallery

    async def reload_gallery(self) -> int:
        """Rebuild the in-RAM gallery from disk. Returns the number of identities.

        Called by ``perception.face_id.reload_gallery`` after the core has
        forgotten or enrolled someone in the same database file.
        """
        gallery = await asyncio.to_thread(self.load_gallery)
        self._gallery = gallery
        return len(gallery.person_ids)

    def load_gallery(self) -> Gallery:
        """Build the in-RAM gallery from the stored embeddings of this model."""
        rows = self.connect().execute(
            "SELECT e.person_id, e.vec, p.name FROM face_embeddings e "
            "LEFT JOIN persons p ON p.id = e.person_id WHERE e.model = ?",
            (self.model,),
        ).fetchall()
        gallery = Gallery()
        for row in rows:
            gallery.add(row["person_id"], decode_vector(row["vec"]), row["name"])
        return gallery

    async def add_face_embedding(
        self, person_id: str, model: str, vec: Any, quality: float
    ) -> None:
        vector = np.asarray(vec, dtype=np.float32).reshape(-1)
        await asyncio.to_thread(
            self._write,
            "INSERT INTO face_embeddings (person_id, model, dim, vec, quality, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (person_id, model, int(vector.size), encode_vector(vector), float(quality), time.time()),
        )
        self._gallery = None

    async def match_face(self, vec: Any) -> tuple[str, float] | None:
        match = self.gallery().match(np.asarray(vec, dtype=np.float32))
        if match.person_id is None or match.score < UNCERTAIN_THRESHOLD:
            return None
        return match.person_id, match.score

    # -- persons -----------------------------------------------------------

    async def get_person(self, person_id: str) -> Person | None:
        row = await asyncio.to_thread(
            self._read_one, "SELECT * FROM persons WHERE id = ?", (person_id,)
        )
        return self._person(row)

    async def find_person_by_name(self, name: str) -> Person | None:
        row = await asyncio.to_thread(
            self._read_one,
            "SELECT * FROM persons WHERE name IS NOT NULL AND lower(name) = lower(?) "
            "ORDER BY created_at LIMIT 1",
            (name,),
        )
        return self._person(row)

    async def upsert_person(self, person: Person) -> Person:
        await asyncio.to_thread(
            self._write,
            "INSERT INTO persons (id, name, created_at, last_seen_at, relationship, notes) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "name = excluded.name, last_seen_at = excluded.last_seen_at, "
            "relationship = excluded.relationship, notes = excluded.notes",
            (
                person.id,
                person.name,
                person.created_at,
                person.last_seen_at,
                person.relationship,
                person.notes,
            ),
        )
        self._gallery = None
        return person

    # -- owned by WS1 ------------------------------------------------------

    async def add_fact(self, fact: Fact) -> None:
        raise NotImplementedError("facts belong to WS1's SqliteMemoryStore")

    async def recall(self, query: str, k: int = 5) -> list[str]:
        raise NotImplementedError("recall belongs to WS1's SqliteMemoryStore")

    async def start_episode(self, participants: tuple[str, ...]) -> Episode:
        raise NotImplementedError("episodes belong to WS1's SqliteMemoryStore")

    async def end_episode(self, episode_id: str, summary: str, mood: str | None = None) -> None:
        raise NotImplementedError("episodes belong to WS1's SqliteMemoryStore")

    async def journal(self, text: str, kind: str = "note") -> None:
        raise NotImplementedError("the journal belongs to WS1's SqliteMemoryStore")

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _person(row: sqlite3.Row | None) -> Person | None:
        if row is None:
            return None
        return Person(
            id=row["id"],
            name=row["name"],
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            relationship=row["relationship"],
            notes=row["notes"],
        )

    def _read_one(self, sql: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
        return self.connect().execute(sql, params).fetchone()

    def _write(self, sql: str, params: tuple[Any, ...]) -> None:
        connection = self.connect()
        connection.execute(sql, params)
        connection.commit()
