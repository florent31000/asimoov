"""Attention policy (with hysteresis) and speaker attribution v1.

The robot must not ping-pong its gaze between two faces, and must look at
whoever is talking. Both decisions are made here, without an LLM, from the
`SocialScene` alone.
"""

from __future__ import annotations

from dataclasses import dataclass

from asimoov.contracts.percepts import Bearing

SPEAKER_DIRECTION_TOLERANCE_DEG = 35.0
DISTANCE_WEIGHT = {"near": 0.30, "medium": 0.20, "far": 0.05}


def attribute_speaker(
    people, *, direction: Bearing | None, current_attention: str | None
) -> str | None:
    """Return the track id most likely to be speaking, or None.

    Rules v1, in order: a direction of arrival wins if a visible person sits
    within `SPEAKER_DIRECTION_TOLERANCE_DEG` of it; otherwise a single
    visible person is the speaker; otherwise the person we are already
    attending to keeps the floor; otherwise nobody is attributed (the core
    never guesses a name from silence).
    """
    visible = list(people)
    if not visible:
        return None

    if direction is not None:
        closest = min(visible, key=lambda person: abs(person.bearing.az - direction.az))
        if abs(closest.bearing.az - direction.az) <= SPEAKER_DIRECTION_TOLERANCE_DEG:
            return closest.track_id
        return None

    if len(visible) == 1:
        return visible[0].track_id

    if current_attention and any(person.track_id == current_attention for person in visible):
        return current_attention

    return None


@dataclass(frozen=True)
class AttentionPolicy:
    """Picks who to look at, and refuses to switch too eagerly.

    ``hold_s`` is how long a freshly chosen target is kept even if another
    person scores higher; ``margin`` is how much better a challenger must
    score to take over after that. A person who starts speaking overrides
    both (``speaker_bonus``), because looking at the speaker is the whole
    point.
    """

    hold_s: float = 2.0
    margin: float = 0.15
    speaker_bonus: float = 0.5

    def score(self, person, *, speaker_track_id: str | None) -> float:
        centered = max(0.0, 1.0 - abs(person.bearing.az) / 90.0)
        total = 0.2 + 0.3 * person.confidence + centered * 0.2
        total += DISTANCE_WEIGHT.get(person.distance_class, 0.1)
        if person.track_id == speaker_track_id:
            total += self.speaker_bonus
        return total

    def choose(
        self,
        people,
        *,
        current: str | None,
        speaker_track_id: str | None,
        now: float,
        chosen_at: float,
    ) -> str | None:
        """Return the track id to attend to, applying hysteresis."""
        visible = list(people)
        if not visible:
            return None

        ranked = sorted(
            visible, key=lambda person: self.score(person, speaker_track_id=speaker_track_id), reverse=True
        )
        best = ranked[0]
        held = next((person for person in visible if person.track_id == current), None)
        if held is None:
            return best.track_id
        if best.track_id == held.track_id:
            return held.track_id

        if best.track_id == speaker_track_id:
            return best.track_id
        if now - chosen_at < self.hold_s:
            return held.track_id

        best_score = self.score(best, speaker_track_id=speaker_track_id)
        held_score = self.score(held, speaker_track_id=speaker_track_id)
        return best.track_id if best_score > held_score + self.margin else held.track_id
