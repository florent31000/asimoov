"""Initiative: greet once, respect cooldowns and quiet hours."""

from __future__ import annotations

import time

from asimoov.contracts.percepts import Bearing
from asimoov.contracts.persona import Initiative
from asimoov.core.mind.initiative import InitiativePolicy, humanize_ago
from asimoov.core.scene.scene import Presence, SocialScene

NOW = 1758290400.0


def scene(*people, speech_active=False):
    return SocialScene(ts=NOW, people=tuple(people), speech_active=speech_active)


def person(track_id="t1", *, person_id=None, name=None, confidence=0.9):
    return Presence(
        track_id=track_id,
        bearing=Bearing(az=0.0, el=0.0),
        confidence=confidence,
        person_id=person_id,
        name=name,
        first_seen=NOW,
        last_seen=NOW,
    )


def policy(**kwargs):
    return InitiativePolicy(Initiative(**kwargs))


def test_an_unknown_arrival_is_greeted_once():
    engine = policy(cooldown_s=120)
    proposal = engine.consider(scene(person()), now=NOW)
    assert proposal is not None
    assert "do not know" in proposal.text
    assert "ask their name" in proposal.instructions

    engine.note_fired(proposal, NOW)
    assert engine.consider(scene(person()), now=NOW + 1) is None


def test_a_known_person_is_greeted_by_name_with_their_last_visit():
    engine = policy()
    proposal = engine.consider(
        scene(person(person_id="person:sam")),
        now=NOW,
        names={"person:sam": "Sam"},
        last_seen={"person:sam": NOW - 3 * 24 * 3600},
    )
    assert "Sam just arrived" in proposal.text
    assert "3 days ago" in proposal.text


def test_the_cooldown_blocks_a_second_greeting():
    engine = policy(cooldown_s=120, level="medium")
    first = engine.consider(scene(person("t1")), now=NOW)
    engine.note_fired(first, NOW)
    assert engine.consider(scene(person("t2")), now=NOW + 60) is None
    assert engine.consider(scene(person("t2")), now=NOW + 121) is not None


def test_the_level_scales_the_cooldown():
    assert policy(cooldown_s=100, level="low").cooldown_s == 200
    assert policy(cooldown_s=100, level="high").cooldown_s == 50


def test_nothing_fires_during_quiet_hours(monkeypatch):
    engine = policy(quiet_hours=("22:00", "08:00"))
    night = time.mktime(time.struct_time((2026, 9, 19, 23, 30, 0, 0, 0, -1)))
    day = time.mktime(time.struct_time((2026, 9, 19, 12, 0, 0, 0, 0, -1)))
    assert engine.in_quiet_hours(night) is True
    assert engine.in_quiet_hours(day) is False
    assert engine.consider(scene(person()), now=night) is None


def test_nothing_fires_while_someone_is_speaking():
    engine = policy()
    assert engine.consider(scene(person(), speech_active=True), now=NOW) is None


def test_greet_on_arrival_can_be_switched_off():
    engine = policy(greet_on_arrival=False)
    assert engine.consider(scene(person()), now=NOW) is None


def test_a_barely_seen_person_is_not_greeted():
    engine = policy()
    assert engine.consider(scene(person(confidence=0.1)), now=NOW) is None


def test_a_track_can_be_greeted_again_after_it_is_forgotten():
    engine = policy(cooldown_s=0)
    proposal = engine.consider(scene(person("t1")), now=NOW)
    engine.note_fired(proposal, NOW)
    assert engine.consider(scene(person("t1")), now=NOW + 5) is None
    engine.forget_track("t1")
    assert engine.consider(scene(person("t1")), now=NOW + 6) is not None


def test_humanize_ago():
    assert humanize_ago(10) == "just now"
    assert humanize_ago(600) == "10 minutes ago"
    assert humanize_ago(7200) == "2 hours ago"
    assert humanize_ago(3 * 24 * 3600) == "3 days ago"


def test_the_greeting_follows_the_persona_language():
    english = policy()
    french = policy()
    french.language = "fr"

    known = person("t1", person_id="person:sam", name="Sam")
    assert "just arrived" in english.consider(scene(known), now=NOW).text
    proposal = french.consider(scene(known), now=NOW)
    assert "vient d'arriver" in proposal.text
    assert proposal.instructions.startswith("Salue Sam")

    unknown = french.consider(scene(person("t2")), now=NOW)
    assert "ne connais pas" in unknown.text
    assert "prenom" in unknown.instructions


def test_an_unsupported_language_falls_back_to_english():
    engine = policy()
    engine.language = "de"
    assert "just arrived" in engine.consider(scene(person("t9", name="Ana")), now=NOW).text


def test_humanize_ago_in_french():
    assert humanize_ago(10, "fr") == "a l'instant"
    assert humanize_ago(600, "fr-FR") == "il y a 10 minutes"
    assert humanize_ago(3 * 24 * 3600, "fr") == "il y a 3 jours"
