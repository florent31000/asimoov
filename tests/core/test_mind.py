"""`Mind` against `InMemoryMemoryStore`: the fake must carry the same duck-typed
surface as `SqliteMemoryStore`, since `Mind` calls `touch_person`/`facts_for`
on `self.memory` unconditionally, not just the frozen `MemoryStore` ABC.
"""

from __future__ import annotations

from asimoov.contracts.fakes import FakeBody, InMemoryMemoryStore
from asimoov.contracts.memory import Fact, Person
from asimoov.contracts.persona import Persona
from asimoov.core.bus.local import LocalBus
from asimoov.core.mind.injector import Injector
from asimoov.core.mind.mind import Mind

NOW = 1758290400.0
PERSON_ID = "person:sam"


async def _deliver(text: str) -> None:
    return None


def _mind(memory) -> Mind:
    bus = LocalBus(src="test")
    return Mind(
        bus=bus,
        persona=Persona(name="Test", language="en", identity="A test robot."),
        body_manifest=FakeBody().manifest,
        injector=Injector(_deliver, bus=bus, clock=lambda: NOW),
        memory=memory,
        clock=lambda: NOW,
    )


async def test_a_tick_with_a_linked_person_runs_against_the_in_memory_store():
    """`_note_identity` (`touch_person`) and `refresh_memories` (`facts_for`)
    must not require `SqliteMemoryStore`: the fake used everywhere else in
    the test suite has to carry them too (review: contracts v1.4 gap).
    """
    memory = InMemoryMemoryStore()
    await memory.upsert_person(Person(id=PERSON_ID, name="Sam", created_at=1.0))
    await memory.add_fact(Fact(person_id=PERSON_ID, text="Sam likes strawberries", ts=1.0))
    await memory.add_fact(Fact(person_id=PERSON_ID, text="Sam builds an InMoov hand", ts=2.0))

    mind = _mind(memory)
    mind.tracker.apply(
        "person_seen",
        {
            "track_id": "t1",
            "person_id": PERSON_ID,
            "bearing": {"az": 0.0, "el": 0.0},
            "confidence": 0.9,
        },
        now=NOW,
    )

    await mind.tick()

    assert mind.memories == (
        "Sam: Sam builds an InMoov hand",
        "Sam: Sam likes strawberries",
    )
    person = await memory.get_person(PERSON_ID)
    assert person is not None
    assert person.last_seen_at == NOW
