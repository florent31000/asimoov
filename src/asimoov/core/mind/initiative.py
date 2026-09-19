"""Initiative: the few cases where the robot speaks first.

V1 has one rule -- greet someone who just arrived -- plus the guards that
make it liveable: a per-person cooldown, a global cooldown scaled by the
persona's initiative level, and quiet hours during which the robot stays
silent (plan.md section 4.3, `persona.initiative`).

The proposal's text and instructions are written in ``persona.language``
(see `phrases`): an injection lands mid-conversation, so it must not switch
the robot's language for a turn.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from asimoov.contracts.persona import Initiative
from asimoov.core.clock import Clock, wall_clock
from asimoov.core.mind.phrases import DEFAULT_LANGUAGE, phrases

LEVEL_COOLDOWN_FACTOR = {"low": 2.0, "medium": 1.0, "high": 0.5}
GREET_MIN_CONFIDENCE = 0.4


def humanize_ago(seconds: float, language: str = DEFAULT_LANGUAGE) -> str:
    """Coarse, human-readable age: ``just now``, ``2 hours ago``, ``3 days ago``."""
    table = phrases(language)
    if seconds < 90:
        return table["just_now"]
    minutes = seconds / 60
    if minutes < 90:
        return table["minutes_ago"].format(n=round(minutes))
    hours = minutes / 60
    if hours < 36:
        return table["hours_ago"].format(n=round(hours))
    return table["days_ago"].format(n=round(hours / 24))


@dataclass(frozen=True)
class InitiativeProposal:
    """One thing the robot wants to do on its own initiative."""

    kind: str
    text: str
    instructions: str
    track_id: str | None = None
    person_id: str | None = None


@dataclass
class InitiativePolicy:
    """Decides whether to speak first, and refuses to do it twice."""

    initiative: Initiative
    language: str = DEFAULT_LANGUAGE
    greeted_tracks: set[str] = field(default_factory=set)
    greeted_persons: dict[str, float] = field(default_factory=dict)
    last_fired_at: float = 0.0
    #: The same clock the mind reads, so a replay stamps the recorded time
    #: instead of silently falling back to wall time (review, minor).
    clock: Clock = wall_clock

    @property
    def cooldown_s(self) -> float:
        factor = LEVEL_COOLDOWN_FACTOR.get(self.initiative.level, 1.0)
        return self.initiative.cooldown_s * factor

    def in_quiet_hours(self, now: float | None = None) -> bool:
        """True inside the persona's ``quiet_hours`` window (local time)."""
        window = self.initiative.quiet_hours
        if not window:
            return False
        start, end = window
        current = time.strftime("%H:%M", time.localtime(self.clock() if now is None else now))
        if start <= end:
            return start <= current < end
        return current >= start or current < end

    def consider(
        self,
        scene,
        *,
        now: float | None = None,
        names: dict[str, str] | None = None,
        last_seen: dict[str, float] | None = None,
    ) -> InitiativeProposal | None:
        """Return at most one proposal for this tick, or None.

        ``names`` and ``last_seen`` come from the memory store, keyed by
        ``person_id``: the mind resolves them so this policy stays pure.
        """
        now = self.clock() if now is None else now
        if not self.initiative.greet_on_arrival or self.in_quiet_hours(now):
            return None
        if scene.speech_active or now - self.last_fired_at < self.cooldown_s:
            return None

        for person in scene.people:
            if person.track_id in self.greeted_tracks or person.confidence < GREET_MIN_CONFIDENCE:
                continue
            person_id = person.person_id
            if person_id and now - self.greeted_persons.get(person_id, 0.0) < self.cooldown_s:
                continue

            table = phrases(self.language)
            name = person.name or (names or {}).get(person_id or "")
            if name:
                seen_at = (last_seen or {}).get(person_id or "")
                when = (
                    table["arrival_when"].format(ago=humanize_ago(now - seen_at, self.language))
                    if seen_at
                    else ""
                )
                text = table["arrival_known"].format(
                    stamp=self._stamp(now), name=name, when=when
                )
                instructions = table["greet_known"].format(name=name)
            else:
                text = table["arrival_unknown"].format(stamp=self._stamp(now))
                instructions = table["greet_unknown"]
            return InitiativeProposal(
                kind="greet",
                text=text,
                instructions=instructions,
                track_id=person.track_id,
                person_id=person_id,
            )
        return None

    def note_fired(self, proposal: InitiativeProposal, now: float | None = None) -> None:
        """Record that ``proposal`` was acted on, so it does not fire again."""
        now = self.clock() if now is None else now
        self.last_fired_at = now
        if proposal.track_id:
            self.greeted_tracks.add(proposal.track_id)
        if proposal.person_id:
            self.greeted_persons[proposal.person_id] = now

    def forget_track(self, track_id: str) -> None:
        """Allow greeting this track again (it left and may come back later)."""
        self.greeted_tracks.discard(track_id)

    def _stamp(self, now: float) -> str:
        return time.strftime(phrases(self.language)["perception_stamp"], time.localtime(now))
