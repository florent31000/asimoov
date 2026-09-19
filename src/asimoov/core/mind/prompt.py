"""Building the system prompt: persona + body + scene + memory.

`{name}` and `{body_description}` in `persona.identity` are filled in here
(plan.md section 4.3): the same persona file therefore describes a
quadruped, a bust or an on-screen avatar without being rewritten.
"""

from __future__ import annotations

from asimoov.contracts.body import BodyManifest
from asimoov.contracts.persona import Persona
from asimoov.core.scene.scene import SocialScene

BODY_KIND_PHRASES = {
    "quadruped": "a four-legged robot that can walk and turn",
    "humanoid_bust": "a humanoid bust with arms and a head",
    "virtual": "a face on a screen, with no body to move",
    "wheeled": "a wheeled robot that can drive around",
    "other": "a robot",
}
CAPABILITY_PHRASES = {
    "locomotion.planar": "move around",
    "locomotion.turn": "turn in place",
    "gaze.pan_tilt": "turn your head to look at people",
    "gaze.body_yaw": "turn your whole body to look at people",
    "face.screen": "show expressions on a screen",
    "face.jaw": "move a jaw when you speak",
    "face.eyelids": "blink",
    "face.leds": "light up",
    "audio.in": "hear",
    "audio.out": "speak out loud",
    "camera.front": "see what is in front of you",
    "touch.head": "feel a touch on your head",
    "battery": "feel your own battery level",
}


def body_description(manifest: BodyManifest) -> str:
    """One sentence describing this body, injected as `{body_description}`."""
    kind = BODY_KIND_PHRASES.get(manifest.kind_of_body, BODY_KIND_PHRASES["other"])
    abilities = [
        CAPABILITY_PHRASES[capability]
        for capability in manifest.capabilities
        if capability in CAPABILITY_PHRASES
    ]
    gestures = sorted(
        capability.split(".", 1)[1].replace("_", " ")
        for capability in manifest.capabilities
        if capability.startswith("gesture.")
    )
    sentence = f"Your body is {kind}."
    if abilities:
        sentence += " You can " + ", ".join(abilities) + "."
    if gestures:
        sentence += " Gestures you can perform: " + ", ".join(gestures) + "."
    return sentence


def describe_scene(scene: SocialScene | None) -> str:
    """Plain-text description of who is present, for the prompt or an injection."""
    if scene is None or not scene.people:
        return "Nobody is visible right now."
    parts = []
    for person in scene.people:
        who = person.name or ("someone you do not know" if not person.person_id else "someone you have met before")
        side = "in front of you"
        if person.bearing.az > 15:
            side = "to your left"
        elif person.bearing.az < -15:
            side = "to your right"
        marks = [side, person.distance_class]
        if person.track_id == scene.speaker_track_id:
            marks.append("speaking")
        if person.track_id == scene.attention_track_id:
            marks.append("you are looking at them")
        parts.append(f"{who} ({', '.join(marks)})")
    return "Present: " + "; ".join(parts) + "."


def build_system_prompt(
    persona: Persona,
    body: BodyManifest,
    *,
    scene: SocialScene | None = None,
    memories: tuple[str, ...] = (),
    fragments: tuple[str, ...] = (),
) -> str:
    """Assemble the system prompt sent to the voice provider."""
    identity = persona.identity.replace("{name}", persona.name).replace(
        "{body_description}", body_description(body)
    )
    blocks = [identity.strip()]

    if persona.traits:
        blocks.append("Traits: " + ", ".join(persona.traits) + ".")
    if persona.speaking_style.strip():
        blocks.append("How you speak:\n" + persona.speaking_style.strip())

    relationships = persona.relationships
    if relationships.owners:
        blocks.append(
            f"The people you live with: {', '.join(relationships.owners)}. "
            f"With them: {relationships.owners_style}. "
            f"With anyone else: {relationships.strangers_style}."
        )
    elif relationships.strangers_style:
        blocks.append(f"With people you do not know: {relationships.strangers_style}.")

    if persona.rules:
        blocks.append("Rules:\n" + "\n".join(f"- {rule}" for rule in persona.rules))

    all_fragments = tuple(persona.fragments) + tuple(fragments)
    if all_fragments:
        blocks.append("\n".join(all_fragments))

    blocks.append(describe_scene(scene))
    if memories:
        blocks.append("What you remember:\n" + "\n".join(f"- {line}" for line in memories))

    blocks.append(f"Answer in {persona.language}.")
    return "\n\n".join(block for block in blocks if block.strip())
