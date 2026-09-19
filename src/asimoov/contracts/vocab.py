"""Versioned, closed vocabularies shared by every ASIMOOV contract.

Frozen as part of contracts v1 (see ``CONTRACTS_FROZEN.md``). Changes are
additive only: a new value may be appended to a tuple/set below and the
version constant bumped, but no existing value may be removed or renumbered.
Apps may extend the percept and topic namespaces under the reserved
``x.<app_name>.*`` prefix; the vocabularies below never grow to include
app-specific names.
"""

from __future__ import annotations

VOCAB_VERSION = 1

# ---------------------------------------------------------------------------
# Envelope kinds (envelope.v1 "kind" field)
# ---------------------------------------------------------------------------

ENVELOPE_KINDS: tuple[str, ...] = (
    "percept",
    "state",
    "cmd",
    "reply",
    "frame",
    "log",
    "metric",
)

# ---------------------------------------------------------------------------
# Topic prefixes (envelope.v1 "topic" field). Binary "frame.*" topics are
# never subscribed to by the core; everything else is JSON text.
# ---------------------------------------------------------------------------

TOPIC_PREFIXES: tuple[str, ...] = (
    "percept.",
    "scene.",
    "face.",
    "body.",
    "voice.",
    "mind.",
    "metric.",
    "log",
    "frame.",
    "x.",
)

# ---------------------------------------------------------------------------
# Percept types (percept.v1). Apps add their own under "x.<app>.<name>",
# which is not part of this closed vocabulary.
# ---------------------------------------------------------------------------

PERCEPT_TYPES: tuple[str, ...] = (
    "person_seen",
    "person_lost",
    "speech_started",
    "speech_ended",
    "utterance",
    "touched",
    "battery",
    "body_state",
    "sound_event",
    "app_event",
)

# ---------------------------------------------------------------------------
# Emotions (face_state.v1 "emotion" field and persona.v1 "emotions" list).
# ---------------------------------------------------------------------------

EMOTIONS: tuple[str, ...] = (
    "neutral",
    "happy",
    "excited",
    "curious",
    "annoyed",
    "sad",
    "angry",
    "love",
    "sleeping",
)

# ---------------------------------------------------------------------------
# Duration classes (behavior.v1 / tools.v1 "duration_class").
# ---------------------------------------------------------------------------

DURATION_CLASSES: tuple[str, ...] = ("instant", "short", "long")

# Upper bound in milliseconds for "instant" and "short"; "long" has no bound
# and always replies asynchronously (see contracts/behaviors.py).
DURATION_CLASS_MAX_MS: dict[str, int | None] = {
    "instant": 300,
    "short": 1500,
    "long": None,
}

# ---------------------------------------------------------------------------
# Distance classes (percept.v1 "person_seen.distance_class").
# ---------------------------------------------------------------------------

DISTANCE_CLASSES: tuple[str, ...] = ("near", "medium", "far")

# ---------------------------------------------------------------------------
# Kinds of body (body.v1 "kind_of_body").
# ---------------------------------------------------------------------------

BODY_KINDS: tuple[str, ...] = ("quadruped", "humanoid_bust", "virtual", "wheeled", "other")

# ---------------------------------------------------------------------------
# Capabilities (body.v1 "capabilities" / behavior.v1 "requires").
# Base capabilities plus one "gesture.<name>" per name in GESTURES.
# ---------------------------------------------------------------------------

GESTURES: tuple[str, ...] = (
    "wave",
    "sit",
    "lie_down",
    "stand",
    "stretch",
    "dance",
    "heart",
    "nod",
    "shake_head",
    "hand_right",
    "hand_left",
    "point",
    # InMoov single-finger test bench (plan.md section 4.9): the first real
    # hardware capability, exposed as "gesture.finger_demo".
    "finger_demo",
)

_BASE_CAPABILITIES: tuple[str, ...] = (
    "locomotion.planar",
    "locomotion.turn",
    "gaze.pan_tilt",
    "gaze.body_yaw",
    "face.screen",
    "face.jaw",
    "face.eyelids",
    "face.leds",
    "audio.out",
    "audio.in",
    "camera.front",
    "touch.head",
    "battery",
)

CAPABILITIES: frozenset[str] = frozenset(
    _BASE_CAPABILITIES + tuple(f"gesture.{name}" for name in GESTURES)
)


def is_capability(value: str) -> bool:
    """Return True if ``value`` is a known capability in vocab v1."""
    return value in CAPABILITIES


def is_percept_type(value: str) -> bool:
    """Return True if ``value`` is a closed-vocabulary percept type.

    App-defined percepts (``app_event`` payloads or raw topics under
    ``x.<app>.*``) are not part of this check.
    """
    return value in PERCEPT_TYPES


def is_emotion(value: str) -> bool:
    """Return True if ``value`` is a known emotion in vocab v1."""
    return value in EMOTIONS


def is_envelope_kind(value: str) -> bool:
    """Return True if ``value`` is a known envelope kind in vocab v1."""
    return value in ENVELOPE_KINDS


# ---------------------------------------------------------------------------
# Canonical topic names (plan.md section 4.3). TOPIC_PREFIXES above remains
# the closed set of prefixes; TOPICS names the specific built-in topics
# every workstream should reference by constant rather than by string
# literal. Exact topics, not patterns: use `Bus.subscribe` with a trailing
# ``*`` (see contracts.bus.Bus) to match a whole prefix.
# ---------------------------------------------------------------------------


class TOPICS:
    """Canonical topic name constants from plan.md section 4.3."""

    SCENE_STATE = "scene.state"
    FACE_STATE = "face.state"
    BODY_CMD = "body.cmd"
    BODY_REPLY = "body.reply"
    BODY_HEALTH = "body.health"
    VOICE_EVENT = "voice.event"
    MIND_INJECTION = "mind.injection"
    LOG = "log"

    # Prefixes (see TOPIC_PREFIXES): every topic under these is a `percept`,
    # `metric` or binary `frame` respectively. `COMPONENT_HEALTH_PATTERN`
    # documents the naming convention health topics follow (`body.health`
    # today; other components publishing their own health snapshot, e.g. a
    # future `perception.health`, follow the same `<component>.health`
    # shape), not a closed enum of topics.
    PERCEPT_PREFIX = "percept."
    METRIC_PREFIX = "metric."
    FRAME_PREFIX = "frame."
    COMPONENT_HEALTH_PATTERN = "component.health"


# ---------------------------------------------------------------------------
# App permissions (app.v1 "permissions" list). Versioned and closed like the
# other vocabularies above: apps request a subset of these, robots grant
# them per-app in `robot.yaml: apps_permissions` (app_manifest.py).
# ---------------------------------------------------------------------------

APP_PERMISSIONS_VERSION = 1

APP_PERMISSIONS: tuple[str, ...] = (
    "camera.frames",
    "llm.vision",
    "memory.read",
    "memory.write",
    "persons.read",
    "persons.write",
    "mail.send",
    "network.outbound",
    "body.motion",
    "body.gesture",
    "audio.in",
    "audio.out",
)


def is_app_permission(value: str) -> bool:
    """Return True if ``value`` is a known app permission in vocab v1."""
    return value in APP_PERMISSIONS
