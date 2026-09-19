"""SQLite memory store: persons, facts, FTS recall, face matching, forgetting."""

from __future__ import annotations

import numpy as np
import pytest

from asimoov.contracts.memory import Fact, Person
from asimoov.core.memory.sqlite_store import SqliteMemoryStore, person_id_for_name
from asimoov.core.memory.summarizer import EpisodeSummarizer


@pytest.fixture
async def store(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    await store.open()
    try:
        yield store
    finally:
        await store.close()


def test_person_id_is_a_stable_slug():
    assert person_id_for_name("Sam") == "person:sam"
    assert person_id_for_name("Sam") == person_id_for_name("sam")
    assert person_id_for_name("Éloïse Martin") == "person:eloise-martin"
    assert person_id_for_name("Sam", suffix=2) == "person:sam-2"
    assert person_id_for_name("???") == "person:unknown"


async def test_upsert_and_find_a_person(store):
    person = Person(id="person:sam", name="Sam", created_at=1.0)
    await store.upsert_person(person)
    assert (await store.get_person("person:sam")).name == "Sam"
    assert (await store.find_person_by_name("SAM")).id == "person:sam"
    assert await store.find_person_by_name("nobody") is None

    await store.upsert_person(Person(id="person:sam", name="Samuel", created_at=1.0))
    assert (await store.get_person("person:sam")).name == "Samuel"


async def test_touch_person_updates_last_seen(store):
    await store.upsert_person(Person(id="p", name="P", created_at=1.0))
    await store.touch_person("p", 1234.0)
    assert (await store.get_person("p")).last_seen_at == 1234.0


async def test_facts_are_stored_and_recalled(store):
    await store.upsert_person(Person(id="person:sam", name="Sam", created_at=1.0))
    await store.add_fact(Fact(person_id="person:sam", text="Sam builds an InMoov hand"))
    await store.add_fact(Fact(person_id="person:sam", text="Sam likes strawberries"))

    assert len(await store.facts_for("person:sam")) == 2
    assert "Sam builds an InMoov hand" in await store.recall("InMoov")
    assert await store.recall("quantum") == []


async def test_recall_survives_punctuation(store):
    await store.upsert_person(Person(id="p", name="P", created_at=1.0))
    await store.add_fact(Fact(person_id="p", text="P loves the sea"))
    assert await store.recall('sea" OR x*') == ["P loves the sea"]


async def test_episodes_and_journal_feed_recall(store):
    episode = await store.start_episode(("person:sam",))
    await store.end_episode(episode.id, "Talked about robots with Sam.", mood="happy")
    await store.journal("The house was quiet tonight.")

    assert "Talked about robots with Sam." in await store.recall("robots")
    assert "The house was quiet tonight." in await store.recall("quiet")

    with pytest.raises(KeyError):
        await store.end_episode(episode.id, "again")


async def test_face_matching_uses_cosine_similarity(store):
    await store.upsert_person(Person(id="person:sam", name="Sam", created_at=1.0))
    vector = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    await store.add_face_embedding("person:sam", "mobilefacenet", vector, 0.9)

    close = np.array([0.95, 0.1, 0.0, 0.0], dtype=np.float32)
    match = await store.match_face(close)
    assert match is not None and match[0] == "person:sam"
    assert match[1] > 0.9

    assert await store.match_face(np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)) is None


async def test_match_face_on_an_empty_gallery(store):
    assert await store.match_face(np.zeros(4, dtype=np.float32)) is None


async def test_forget_a_person_removes_everything(store):
    await store.upsert_person(Person(id="person:sam", name="Sam", created_at=1.0))
    await store.add_fact(Fact(person_id="person:sam", text="Sam builds robots"))
    await store.add_face_embedding("person:sam", "m", np.ones(4, dtype=np.float32), 0.9)

    assert await store.delete_person("person:sam") is True
    assert await store.get_person("person:sam") is None
    assert await store.recall("robots") == []
    assert await store.match_face(np.ones(4, dtype=np.float32)) is None
    assert await store.delete_person("person:sam") is False


async def test_forgetting_a_person_cascades_to_their_episodes(store):
    await store.upsert_person(Person(id="person:sam", name="Sam", created_at=1.0))
    await store.upsert_person(Person(id="person:sammy", name="Sammy", created_at=1.0))
    await store.add_fact(Fact(person_id="person:sam", text="Sam builds robots"))
    mine = await store.start_episode(("person:sam", "person:sammy"))
    await store.end_episode(mine.id, "Sam talked about strawberries")
    theirs = await store.start_episode(("person:sammy",))
    await store.end_episode(theirs.id, "Sammy talked about bicycles")
    await store.journal("the house was quiet")

    assert await store.delete_person("person:sam") is True
    assert await store.recall("strawberries") == []
    assert await store.recall("robots") == []
    assert await store.recall("bicycles") == ["Sammy talked about bicycles"]
    assert await store.recall("quiet") == ["the house was quiet"]
    episodes = await store._run("SELECT id FROM episodes", fetch="all")  # noqa: SLF001
    assert [row["id"] for row in episodes] == [theirs.id]


async def test_the_gallery_survives_a_reopen(tmp_path):
    path = tmp_path / "memory.db"
    store = SqliteMemoryStore(path)
    await store.open()
    await store.upsert_person(Person(id="p", name="P", created_at=1.0))
    await store.add_face_embedding("p", "m", np.array([0.0, 1.0], dtype=np.float32), 0.8)
    await store.close()

    reopened = SqliteMemoryStore(path)
    await reopened.open()
    try:
        match = await reopened.match_face(np.array([0.1, 0.9], dtype=np.float32))
        assert match is not None and match[0] == "p"
    finally:
        await reopened.close()


async def test_using_the_store_before_open_is_an_error(tmp_path):
    with pytest.raises(RuntimeError, match="open"):
        await SqliteMemoryStore(tmp_path / "x.db").get_person("p")


async def test_summarizer_uses_the_injected_callable():
    async def generate(prompt: str) -> str:
        assert "Sam" in prompt
        return "Sam talked about robots."

    assert await EpisodeSummarizer(generate).summarize(["Sam: hi", "Robot: hello"]) == (
        "Sam talked about robots."
    )


async def test_summarizer_without_a_callable_is_explicitly_extractive():
    summary = await EpisodeSummarizer().summarize(["a", "b"])
    assert summary.startswith("(extract)")
    assert await EpisodeSummarizer().summarize([]) == ""


async def test_summarizer_falls_back_when_the_callable_fails():
    async def broken(prompt: str) -> str:
        raise RuntimeError("no model")

    assert (await EpisodeSummarizer(broken).summarize(["a"])).startswith("(extract)")
