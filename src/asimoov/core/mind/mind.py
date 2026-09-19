"""`Mind`: the slow loop that decides what needs no LLM.

One tick per second (plan.md section 4.4): apply percepts to the scene,
publish it, aim the gaze, blink, evaluate initiative, flush the injector.
Nothing here ever calls a model; it only decides what the model is told and
when it is asked to speak.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from typing import Any

from asimoov.contracts.body import BodyManifest
from asimoov.contracts.envelope import Envelope
from asimoov.contracts.face import FaceGaze, FaceState
from asimoov.contracts.persona import Persona
from asimoov.contracts.vocab import TOPICS, is_emotion
from asimoov.core.clock import Clock, wall_clock
from asimoov.core.mind.initiative import InitiativePolicy
from asimoov.core.mind.injector import Injector
from asimoov.core.mind.phrases import phrases
from asimoov.core.mind.prompt import build_system_prompt
from asimoov.core.scene.scene import SceneTracker, SocialScene

log = logging.getLogger(__name__)

TICK_S = 1.0
FACE_PERIOD_S = 1 / 20
FACE_IDLE_PERIOD_S = 0.5
BLINK_MIN_S = 3.0
BLINK_MAX_S = 6.0
GAZE_AZ_SCALE = 60.0
GAZE_EL_SCALE = 40.0
MEMORY_LINES_PER_PERSON = 3
# How long a `face.overlay` stays mixed in without being refreshed. A body
# animating the face publishes at its own rate; when it stops, the overlay
# lapses and the face is the mind's again, with no "overlay off" message.
OVERLAY_TTL_S = 0.25
OVERLAY_FIELDS = ("emotion", "intensity", "gaze", "lip", "eyelids")


def _clamp(value: float, limit: float = 1.0) -> float:
    return max(-limit, min(limit, value))


class Mind:
    """The robot's slow loop, wired to the bus and the voice provider."""

    def __init__(
        self,
        *,
        bus,
        persona: Persona,
        body_manifest: BodyManifest,
        injector: Injector,
        tracker: SceneTracker | None = None,
        initiative: InitiativePolicy | None = None,
        memory=None,
        request_response=None,
        clock: Clock = wall_clock,
        tick_s: float = TICK_S,
        fragments: tuple[str, ...] = (),
    ) -> None:
        self.bus = bus
        self.persona = persona
        self.body_manifest = body_manifest
        self.injector = injector
        self.tracker = tracker or SceneTracker()
        self.initiative = initiative or InitiativePolicy(
            persona.initiative, language=persona.language
        )
        self.memory = memory
        self.request_response = request_response
        self.clock = clock
        self.tick_s = tick_s
        self.fragments = fragments

        self.scene: SocialScene = self.tracker.snapshot(clock())
        self.memories: tuple[str, ...] = ()
        self.transcript: list[str] = []
        self.emotion = "neutral"
        self.intensity = 1.0
        self.talking = False
        self.lip = 0.0
        self._pending_instructions: str | None = None
        self._last_scene_payload: dict[str, Any] | None = None
        self._last_face_payload: dict[str, Any] | None = None
        self._next_blink = clock() + random.uniform(BLINK_MIN_S, BLINK_MAX_S)
        self._blink_now = False
        self._names: dict[str, str] = {}
        self._last_seen: dict[str, float] = {}
        self._resolved: set[str] = set()
        self._uncertain_tracks: set[str] = set()
        self._tasks: list[asyncio.Task] = []
        self._subscriptions: list[Any] = []
        self._overlay: dict[str, Any] = {}
        self._overlay_until = 0.0

    # -- lifecycle --------------------------------------------------------

    async def start(self, spawn=None) -> None:
        """Subscribe to percepts and start the tick and face loops.

        ``spawn(name, factory)`` lets the runtime's `Supervisor` own the two
        loops (and restart them); without it the mind runs them itself.
        """
        self._subscriptions = [
            self.bus.subscribe(TOPICS.PERCEPT_PREFIX + "*", self.on_percept),
            self.bus.subscribe(TOPICS.FACE_OVERLAY, self.on_overlay),
        ]
        if spawn is not None:
            spawn("mind.tick", self.tick_forever)
            spawn("mind.face", self.face_forever)
            return
        self._tasks = [
            asyncio.create_task(self.tick_forever(), name="mind.tick"),
            asyncio.create_task(self.face_forever(), name="mind.face"),
        ]

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions = []
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    # -- inputs -----------------------------------------------------------

    async def on_percept(self, envelope: Envelope) -> None:
        """Apply one ``percept.*`` envelope to the scene."""
        percept_type = envelope.topic[len(TOPICS.PERCEPT_PREFIX) :]
        now = self.clock()
        if percept_type == "person_lost":
            self.initiative.forget_track(envelope.data.get("track_id", ""))
        if percept_type == "utterance" and envelope.data.get("final"):
            self.transcript.append(str(envelope.data.get("text", "")))
        if percept_type == "person_seen":
            self._note_uncertain_match(envelope.data, now)
        if self.tracker.apply(percept_type, envelope.data, now=now):
            self.scene = self.tracker.snapshot(now)

    def _note_uncertain_match(self, data: dict[str, Any], now: float) -> None:
        """Tell the model who this may be, once per track, never as a fact.

        Between 0.45 and 0.6 of cosine similarity the recognizer publishes
        ``identity_status: uncertain`` (plan.md section 4.7). The persona
        decides whether to ask; the mind only says what perception saw.
        """
        if data.get("identity_status") != "uncertain":
            return
        track_id = str(data.get("track_id") or "")
        name = str(data.get("candidate_name") or "")
        if not track_id or not name or track_id in self._uncertain_tracks:
            return
        self._uncertain_tracks.add(track_id)
        table = phrases(self.persona.language)
        stamp = time.strftime(table["perception_stamp"], time.localtime(now))
        score = data.get("candidate_score")
        self.injector.submit(
            table["uncertain_match"].format(
                stamp=stamp, name=name, score=f"{float(score):.2f}" if score else "?"
            ),
            now=now,
        )

    async def on_overlay(self, envelope: Envelope) -> None:
        """Mix a body's `face.overlay` into the face for the next `OVERLAY_TTL_S`.

        The mind alone publishes `face.state`; a body that animates the face
        (the avatar nodding, a bust moving its eyes) says so here and the
        mind decides what the whole robot shows.
        """
        payload = envelope.data
        overlay = {key: payload[key] for key in OVERLAY_FIELDS if key in payload}
        if not overlay:
            return
        self._overlay = overlay
        self._overlay_until = self.clock() + float(payload.get("ttl_s", OVERLAY_TTL_S))

    def set_response_active(self, active: bool) -> None:
        """Called by the voice bridge: injections wait for the response to end."""
        self.injector.set_response_active(active)
        self.talking = active

    def set_emotion(self, emotion: str, intensity: float = 1.0) -> None:
        if not is_emotion(emotion):
            raise ValueError(f"unknown emotion: {emotion!r}")
        self.emotion = emotion
        self.intensity = intensity

    def set_lip(self, lip: float) -> None:
        self.lip = max(0.0, min(1.0, lip))

    def bind_person(self, track_id: str, person_id: str, name: str | None) -> bool:
        """Attach an identity to a live track and remember the name."""
        if name:
            self._names[person_id] = name
        bound = self.tracker.bind_person(track_id, person_id, name)
        if bound:
            self.scene = self.tracker.snapshot(self.clock())
        return bound

    # -- outputs ----------------------------------------------------------

    def face_state(self) -> FaceState:
        """The face the whole robot shows right now (screen, Kivy, servos).

        The mind's own face, with the latest `face.overlay` a body published
        mixed on top of it.
        """
        attention = self.scene.attention()
        gaze = FaceGaze(0.0, 0.0)
        if attention is not None:
            gaze = FaceGaze(
                x=_clamp(attention.bearing.az / GAZE_AZ_SCALE),
                y=_clamp(attention.bearing.el / GAZE_EL_SCALE),
            )
        fields: dict[str, Any] = {
            "emotion": self.emotion,
            "intensity": self.intensity,
            "gaze": gaze,
            "lip": self.lip,
            "eyelids": 0.0,
        }
        if self.clock() < self._overlay_until:
            fields.update(self._overlay)
            if isinstance(fields["gaze"], dict):
                fields["gaze"] = FaceGaze(
                    x=_clamp(float(fields["gaze"].get("x", 0.0))),
                    y=_clamp(float(fields["gaze"].get("y", 0.0))),
                )
        return FaceState(**fields, blink=self._blink_now, talking=self.talking, ts=self.clock())

    def system_prompt(self) -> str:
        """The system prompt for the current scene and memories."""
        return build_system_prompt(
            self.persona,
            self.body_manifest,
            scene=self.scene,
            memories=self.memories,
            fragments=self.fragments,
        )

    async def refresh_memories(self) -> tuple[str, ...]:
        """Reload the facts about the people currently present."""
        if self.memory is None:
            return ()
        lines: list[str] = []
        for person in self.scene.known_people():
            name = person.name or self._names.get(person.person_id or "", "")
            facts = await self.memory.facts_for(person.person_id, MEMORY_LINES_PER_PERSON)
            lines.extend(f"{name}: {fact}" if name else fact for fact in facts)
        self.memories = tuple(lines)
        return self.memories

    async def tick(self) -> SocialScene:
        """One decision cycle. Public so tests drive it without the loop."""
        now = self.clock()
        self.scene = self.tracker.tick(now)
        if await self._resolve_identities():
            # Somebody the robot knows just appeared: the facts in the prompt
            # were loaded once at startup, when nobody was there yet (review,
            # medium: "souvenirs du prompt toujours vides").
            await self.refresh_memories()
        await self._publish_scene()

        proposal = self.initiative.consider(
            self.scene, now=now, names=self._names, last_seen=self._last_seen
        )
        if proposal is not None and self.injector.submit(proposal.text, now=now):
            self.initiative.note_fired(proposal, now)
            self._pending_instructions = proposal.instructions

        delivered = await self.injector.pump(now)
        if delivered is not None:
            instructions = self._pending_instructions
            self._pending_instructions = None
            if self.request_response is not None and not self.injector.response_active:
                try:
                    await self.request_response(instructions)
                except RuntimeError:
                    # The model started a response between the injection and
                    # here: skipping is the correct behaviour, not a crash
                    # the supervisor has to swallow (review, blocker 4).
                    log.debug("request_response skipped: a response is already active")
        return self.scene

    async def inject(self, text: str) -> None:
        """Queue a perception/action note for the model (executor completions)."""
        self.injector.submit(text, now=self.clock())

    # -- internals --------------------------------------------------------

    async def _resolve_identities(self) -> bool:
        """Look up the people present. True if one of them is newly known."""
        discovered = False
        for person in self.scene.known_people():
            discovered |= await self._note_identity(person.person_id)
        return discovered

    async def _note_identity(self, person_id: str | None) -> bool:
        if not person_id or self.memory is None or person_id in self._resolved:
            return False
        self._resolved.add(person_id)
        person = await self.memory.get_person(person_id)
        if person is None:
            return False
        if person.name:
            self._names[person_id] = person.name
        if person.last_seen_at:
            self._last_seen[person_id] = person.last_seen_at
        await self.memory.touch_person(person_id, self.clock())
        return True

    async def _publish_scene(self) -> None:
        payload = self.scene.to_dict()
        comparable = {key: value for key, value in payload.items() if key != "ts"}
        if comparable == self._last_scene_payload:
            return
        self._last_scene_payload = comparable
        await self.bus.publish(TOPICS.SCENE_STATE, payload, kind="state")

    async def _publish_face(self) -> bool:
        state = self.face_state()
        payload = state.to_dict()
        comparable = {key: value for key, value in payload.items() if key != "ts"}
        if comparable == self._last_face_payload:
            return False
        self._last_face_payload = comparable
        await self.bus.publish(TOPICS.FACE_STATE, payload, kind="state")
        return True

    async def tick_forever(self) -> None:
        while True:
            await asyncio.sleep(self.tick_s)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("mind tick failed")

    async def face_forever(self) -> None:
        idle_for = 0.0
        while True:
            await asyncio.sleep(FACE_PERIOD_S)
            now = self.clock()
            if now >= self._next_blink:
                self._blink_now = True
                self._next_blink = now + random.uniform(BLINK_MIN_S, BLINK_MAX_S)
            try:
                published = await self._publish_face()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("face publication failed")
                published = False
            finally:
                self._blink_now = False
            idle_for = 0.0 if published else idle_for + FACE_PERIOD_S
            if idle_for >= FACE_IDLE_PERIOD_S:
                idle_for = 0.0
                self._last_face_payload = None
