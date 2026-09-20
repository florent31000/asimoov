"""The tools that are not behaviors: who is here, memory, identity, and stop.

``stop`` deliberately bypasses everything and calls `SafetyGuard.stop_all`
directly (plan.md section 4.4): an emergency stop is not a behavior that
can queue behind a gesture.

``remember_person`` and ``forget_person`` are the two halves of the same
promise: the first only succeeds if perception really enrolled the face,
the second removes the person from the database *and* from the recognizer's
in-memory gallery, so "forget me" is true immediately.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from asimoov.contracts.behaviors import BehaviorStatus
from asimoov.contracts.memory import Fact, Person
from asimoov.contracts.tools import ToolHandler, ToolResult, ToolSpec
from asimoov.core.clock import Clock, wall_clock
from asimoov.core.memory.sqlite_store import person_id_for_name

log = logging.getLogger(__name__)

RECALL_LIMIT = 5
ENROLL_TOPIC = "perception.face_id.enroll"
RELOAD_GALLERY_TOPIC = "perception.face_id.reload_gallery"
ENROLL_TIMEOUT_S = 12.0
RELOAD_TIMEOUT_S = 5.0
NO_PERCEPTION = "no perception"


def build_builtin_tools(
    *,
    scene_provider,
    memory=None,
    mind=None,
    safety=None,
    bus=None,
    clock: Clock = wall_clock,
) -> list[tuple[ToolSpec, ToolHandler]]:
    """Build the builtin tools that the current runtime can actually serve."""
    tools: list[tuple[ToolSpec, ToolHandler]] = []

    async def who_is_here(params: dict[str, Any]) -> ToolResult:
        scene = scene_provider()
        people = []
        for person in scene.people:
            people.append(
                {
                    "name": person.name,
                    "known": bool(person.person_id),
                    "speaking": person.speaking,
                    "where": "left" if person.bearing.az > 15 else "right" if person.bearing.az < -15 else "front",
                    "distance": person.distance_class,
                }
            )
        return ToolResult(status=BehaviorStatus.OK, content={"people": people})

    tools.append(
        (
            ToolSpec(
                name="who_is_here",
                description="List the people you can currently see and whether you know them.",
                params={"type": "object", "properties": {}},
                timeout_s=1.0,
                duration_class="instant",
            ),
            who_is_here,
        )
    )

    if memory is not None:
        tools.append(
            (
                _remember_person_spec(),
                _remember_person(memory, mind, scene_provider, bus, clock),
            )
        )
        tools.append((_forget_person_spec(), _forget_person(memory, scene_provider, bus)))
        tools.append((_remember_fact_spec(), _remember_fact(memory, scene_provider, clock)))
        tools.append((_recall_spec(), _recall(memory)))

    if safety is not None:

        async def stop(params: dict[str, Any]) -> ToolResult:
            await safety.stop_all("tool")
            return ToolResult(status=BehaviorStatus.OK, content={"stopped": True})

        tools.append(
            (
                ToolSpec(
                    name="stop",
                    description="Stop every movement immediately.",
                    params={"type": "object", "properties": {}},
                    timeout_s=1.0,
                    duration_class="instant",
                ),
                stop,
            )
        )
    return tools


def _remember_person_spec() -> ToolSpec:
    return ToolSpec(
        name="remember_person",
        description=(
            "Remember the person you are talking to under this name, so you recognize "
            "them next time."
        ),
        params={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        timeout_s=2.0,
        duration_class="short",
    )


async def _request_perception(bus, topic: str, payload: dict[str, Any], timeout_s: float):
    """Ask the perception process something, or say it is not there.

    Returns the reply envelope's data, or None when no perception module
    answers. A tool that cannot do what it claims must say so, never
    return a fabricated ``ok`` (review, blocker 5).
    """
    if bus is None:
        return None
    try:
        reply = await bus.request(topic, payload, timeout_s=timeout_s)
    except (TimeoutError, asyncio.TimeoutError):
        log.warning("no perception module answered %s within %.0fs", topic, timeout_s)
        return None
    return reply.data


def _remember_person(memory, mind, scene_provider, bus, clock: Clock) -> ToolHandler:
    async def handler(params: dict[str, Any]) -> ToolResult:
        name = str(params.get("name") or "").strip()
        if not name:
            return ToolResult(status=BehaviorStatus.ERROR, reason="a name is required")

        scene = scene_provider()
        track = scene.speaker() or scene.attention()
        if track is None:
            return ToolResult(
                status=BehaviorStatus.ERROR, reason="nobody is visible to remember"
            )

        existing = await memory.find_person_by_name(name)
        if existing is not None:
            person_id = existing.id
        else:
            person_id = person_id_for_name(name)
            suffix = 1
            while True:
                clash = await memory.get_person(person_id)
                if clash is None:
                    break
                suffix += 1
                person_id = person_id_for_name(name, suffix=suffix)

        # Enrol first: remembering a name the robot cannot put a face to is
        # a promise it will not keep. An existing `person_id` means the
        # embeddings join that person instead of creating a twin.
        enrolled = await _request_perception(
            bus,
            ENROLL_TOPIC,
            {"track_id": track.track_id, "person_id": person_id, "name": name},
            ENROLL_TIMEOUT_S,
        )
        if enrolled is None:
            return ToolResult(status=BehaviorStatus.ERROR, reason=NO_PERCEPTION)
        if not enrolled.get("ok"):
            return ToolResult(
                status=BehaviorStatus.ERROR,
                reason=str(enrolled.get("reason") or "enrollment failed"),
            )

        now = clock()
        await memory.upsert_person(
            Person(
                id=person_id,
                name=name,
                created_at=existing.created_at if existing else now,
                last_seen_at=now,
                relationship=existing.relationship if existing else None,
                notes=existing.notes if existing else None,
            )
        )
        bound = mind.bind_person(track.track_id, person_id, name) if mind is not None else False
        await memory.journal(f"Met {name}.", kind="person")
        return ToolResult(
            status=BehaviorStatus.OK,
            content={
                "person_id": person_id,
                "name": name,
                "bound_to_track": bound,
                "samples": enrolled.get("samples", 0),
            },
        )

    return handler


def _forget_person_spec() -> ToolSpec:
    return ToolSpec(
        name="forget_person",
        description=(
            "Forget everything about a person: their name, the facts you know about "
            "them, and their face. Use it when they ask you to."
        ),
        params={
            "type": "object",
            "properties": {"person": {"type": "string", "description": "a name, or 'current'"}},
            "required": ["person"],
        },
        timeout_s=6.0,
        duration_class="short",
    )


def _forget_person(memory, scene_provider, bus) -> ToolHandler:
    async def handler(params: dict[str, Any]) -> ToolResult:
        who = str(params.get("person") or "current").strip()
        if who in ("current", "", "me", "them"):
            scene = scene_provider()
            track = scene.speaker() or scene.attention()
            person_id = track.person_id if track else None
        else:
            person = await memory.find_person_by_name(who)
            person_id = person.id if person else None
        if person_id is None:
            return ToolResult(
                status=BehaviorStatus.ERROR, reason=f"you do not know {who!r}"
            )

        await memory.delete_person(person_id)
        # The recognizer keeps its gallery in RAM: without this the face is
        # still recognized until the next restart (review, medium).
        reloaded = await _request_perception(
            bus, RELOAD_GALLERY_TOPIC, {}, RELOAD_TIMEOUT_S
        )
        return ToolResult(
            status=BehaviorStatus.OK,
            content={"person_id": person_id, "gallery_reloaded": reloaded is not None},
        )

    return handler


def _remember_fact_spec() -> ToolSpec:
    return ToolSpec(
        name="remember_fact",
        description="Remember one fact about a person you know.",
        params={
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "a name, or 'current'"},
                "fact": {"type": "string"},
            },
            "required": ["person", "fact"],
        },
        timeout_s=2.0,
        duration_class="short",
    )


def _remember_fact(memory, scene_provider, clock: Clock) -> ToolHandler:
    async def handler(params: dict[str, Any]) -> ToolResult:
        text = str(params.get("fact") or "").strip()
        who = str(params.get("person") or "current").strip()
        if not text:
            return ToolResult(status=BehaviorStatus.ERROR, reason="a fact is required")

        person_id: str | None = None
        if who in ("current", "", "me", "them"):
            scene = scene_provider()
            track = scene.speaker() or scene.attention()
            person_id = track.person_id if track else None
        else:
            person = await memory.find_person_by_name(who)
            person_id = person.id if person else None

        if person_id is None:
            return ToolResult(
                status=BehaviorStatus.ERROR,
                reason=f"you do not know {who!r} yet: call remember_person first",
            )
        await memory.add_fact(Fact(person_id=person_id, text=text, ts=clock()))
        return ToolResult(status=BehaviorStatus.OK, content={"person_id": person_id})

    return handler


def _recall_spec() -> ToolSpec:
    return ToolSpec(
        name="recall",
        description="Search your own memory for what you know about a topic or a person.",
        params={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        timeout_s=2.0,
        duration_class="short",
    )


def _recall(memory) -> ToolHandler:
    async def handler(params: dict[str, Any]) -> ToolResult:
        query = str(params.get("query") or "").strip()
        if not query:
            return ToolResult(status=BehaviorStatus.ERROR, reason="a query is required")
        snippets = await memory.recall(query, RECALL_LIMIT)
        return ToolResult(status=BehaviorStatus.OK, content={"snippets": snippets})

    return handler
