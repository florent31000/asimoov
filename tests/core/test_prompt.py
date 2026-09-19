"""Prompt building: persona placeholders, body description, scene text."""

from __future__ import annotations

from asimoov.contracts.body import BodyManifest
from asimoov.contracts.fakes import FakeBody
from asimoov.contracts.percepts import Bearing
from asimoov.core.mind.prompt import body_description, build_system_prompt, describe_scene
from asimoov.core.scene.scene import Presence, SocialScene

GO2 = BodyManifest(
    name="go2",
    kind_of_body="quadruped",
    capabilities=("locomotion.planar", "gesture.wave", "gesture.sit", "camera.front"),
)


def test_body_description_follows_the_body():
    text = body_description(GO2)
    assert "four-legged" in text
    assert "move around" in text
    assert "wave" in text and "sit" in text

    assert "face on a screen" in body_description(FakeBody().manifest)


def test_the_persona_placeholders_are_filled(avatar_config):
    prompt = build_system_prompt(avatar_config.persona, GO2)
    assert "{name}" not in prompt
    assert "{body_description}" not in prompt
    assert "Aria" in prompt
    assert "four-legged" in prompt
    assert "Answer in en." in prompt


def test_rules_and_traits_are_included(avatar_config):
    prompt = build_system_prompt(avatar_config.persona, GO2)
    assert "Never perform a physical action" in prompt
    assert "curious" in prompt


def test_memories_and_fragments_are_appended(avatar_config):
    prompt = build_system_prompt(
        avatar_config.persona,
        GO2,
        memories=("Sam: builds an InMoov hand",),
        fragments=("You can describe what you see.",),
    )
    assert "Sam: builds an InMoov hand" in prompt
    assert "You can describe what you see." in prompt


def test_describe_scene_empty():
    assert describe_scene(None) == "Nobody is visible right now."
    assert describe_scene(SocialScene()) == "Nobody is visible right now."


def test_describe_scene_places_people():
    scene = SocialScene(
        people=(
            Presence(track_id="t1", bearing=Bearing(az=40.0, el=0.0), name="Sam", person_id="person:sam"),
            Presence(track_id="t2", bearing=Bearing(az=-40.0, el=0.0)),
        ),
        attention_track_id="t1",
        speaker_track_id="t1",
    )
    text = describe_scene(scene)
    assert "Sam (to your left" in text
    assert "speaking" in text
    assert "someone you do not know (to your right" in text
