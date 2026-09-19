"""The SQLite face store: WS1's `persons` / `face_embeddings` tables."""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from asimoov.contracts.memory import Person
from asimoov.core.memory import SqliteMemoryStore
from asimoov.perception.models import EMBEDDING_MODEL_ID
from asimoov.perception.store import SqliteFaceStore, decode_vector, default_db_path


@pytest.fixture()
def store(tmp_path):
    instance = SqliteFaceStore(tmp_path / "memory.db")
    yield instance
    instance.close()


def unit(*values: float) -> np.ndarray:
    vector = np.array(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


def test_schema_matches_the_plan(store):
    connection = store.connect()
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"persons", "face_embeddings"} <= tables
    columns = {row[1] for row in connection.execute("PRAGMA table_info(face_embeddings)")}
    assert columns == {"person_id", "model", "dim", "vec", "quality", "created_at"}
    columns = {row[1] for row in connection.execute("PRAGMA table_info(persons)")}
    assert columns == {"id", "name", "created_at", "last_seen_at", "relationship", "notes"}


async def test_person_round_trip(store):
    await store.upsert_person(Person(id="p_sam", name="Sam"))
    person = await store.get_person("p_sam")
    assert person.name == "Sam"
    assert await store.find_person_by_name("sam") is not None
    assert await store.get_person("p_nobody") is None


async def test_upsert_updates_the_name(store):
    await store.upsert_person(Person(id="p_sam", name="Sam"))
    await store.upsert_person(Person(id="p_sam", name="Samuel"))
    assert (await store.get_person("p_sam")).name == "Samuel"


async def test_embedding_is_stored_as_float32_blob(store):
    await store.upsert_person(Person(id="p_sam", name="Sam"))
    await store.add_face_embedding("p_sam", EMBEDDING_MODEL_ID, unit(1, 0, 0), 0.8)
    row = store.connect().execute("SELECT * FROM face_embeddings").fetchone()
    assert row["dim"] == 3
    assert row["model"] == EMBEDDING_MODEL_ID
    assert row["quality"] == pytest.approx(0.8)
    assert decode_vector(row["vec"]) == pytest.approx(unit(1, 0, 0))


async def test_match_face_uses_the_gallery(store):
    await store.upsert_person(Person(id="p_sam", name="Sam"))
    await store.add_face_embedding("p_sam", EMBEDDING_MODEL_ID, unit(1, 0), 0.9)
    assert await store.match_face(unit(1, 0)) == ("p_sam", pytest.approx(1.0, abs=1e-6))


async def test_match_face_returns_none_below_the_unknown_threshold(store):
    await store.upsert_person(Person(id="p_sam", name="Sam"))
    await store.add_face_embedding("p_sam", EMBEDDING_MODEL_ID, unit(1, 0), 0.9)
    assert await store.match_face(unit(0.2, 1.0)) is None


async def test_the_gallery_cache_is_invalidated_by_a_write(store):
    assert len(store.gallery()) == 0
    await store.upsert_person(Person(id="p_sam", name="Sam"))
    await store.add_face_embedding("p_sam", EMBEDDING_MODEL_ID, unit(1, 0), 0.9)
    assert len(store.gallery()) == 1


async def test_the_gallery_carries_the_person_name(store):
    await store.upsert_person(Person(id="p_sam", name="Sam"))
    await store.add_face_embedding("p_sam", EMBEDDING_MODEL_ID, unit(1, 0), 0.9)
    assert store.gallery().match(unit(1, 0)).name == "Sam"


async def test_embeddings_of_another_model_are_not_loaded(store):
    await store.upsert_person(Person(id="p_sam", name="Sam"))
    await store.add_face_embedding("p_sam", "some_other_model", unit(1, 0), 0.9)
    assert len(store.gallery()) == 0


async def test_the_database_survives_a_reopen(tmp_path):
    first = SqliteFaceStore(tmp_path / "memory.db")
    await first.upsert_person(Person(id="p_sam", name="Sam"))
    await first.add_face_embedding("p_sam", EMBEDDING_MODEL_ID, unit(1, 0), 0.9)
    first.close()
    second = SqliteFaceStore(tmp_path / "memory.db")
    assert len(second.gallery()) == 1
    second.close()


async def test_ws1_owned_methods_refuse_instead_of_faking(store):
    with pytest.raises(NotImplementedError):
        await store.recall("anything")
    with pytest.raises(NotImplementedError):
        await store.journal("anything")


def test_a_second_connection_can_read_the_same_file(tmp_path):
    store = SqliteFaceStore(tmp_path / "memory.db")
    store.connect()
    store.close()
    connection = sqlite3.connect(str(tmp_path / "memory.db"))
    assert connection.execute("SELECT count(*) FROM face_embeddings").fetchone()[0] == 0
    connection.close()


def test_the_default_database_follows_asimoov_home(monkeypatch, tmp_path):
    monkeypatch.setenv("ASIMOOV_HOME", str(tmp_path))
    assert default_db_path() == tmp_path / "memory.db"
    assert SqliteFaceStore().path == tmp_path / "memory.db"


SHARED_TABLES = {"persons", "face_embeddings", "facts", "episodes", "journal", "mem_fts"}


def test_the_store_opens_the_database_in_wal_mode(store):
    assert store.connect().execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_the_face_store_creates_the_whole_shared_schema(store):
    tables = {
        row[0]
        for row in store.connect().execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert tables >= SHARED_TABLES


@pytest.mark.parametrize("perception_first", [True, False])
async def test_both_stores_share_one_schema_whichever_opens_first(tmp_path, perception_first):
    path = tmp_path / "memory.db"
    face = SqliteFaceStore(path)
    memory = SqliteMemoryStore(path)
    if perception_first:
        face.connect()
        await memory.open()
    else:
        await memory.open()
        face.connect()
    try:
        for connection in (face.connect(), memory._connection):  # noqa: SLF001
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert tables >= SHARED_TABLES
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        await memory.upsert_person(Person(id="p_sam", name="Sam", created_at=1.0))
        assert (await face.get_person("p_sam")).name == "Sam"
    finally:
        face.close()
        await memory.close()
