"""The few sentences the core writes for the model, in the persona's language.

The system prompt is English (it describes the robot to the model), but a
perception note or an initiative instruction lands in the middle of a
conversation: injecting "Someone you do not know just arrived" into a French
conversation makes the robot answer in English for a turn. These templates
follow ``persona.language``; an unsupported language falls back to English.
"""

from __future__ import annotations

DEFAULT_LANGUAGE = "en"

PHRASES: dict[str, dict[str, str]] = {
    "en": {
        "perception_stamp": "[Perception %H:%M]",
        "just_now": "just now",
        "minutes_ago": "{n} minutes ago",
        "hours_ago": "{n} hours ago",
        "days_ago": "{n} days ago",
        "arrival_known": "{stamp} {name} just arrived{when}.",
        "arrival_when": " (last seen {ago})",
        "arrival_unknown": "{stamp} Someone you do not know just arrived.",
        "greet_known": "Greet {name} warmly, in one short sentence.",
        "greet_unknown": (
            "Greet this person you have not met, in one short sentence, and ask their name."
        ),
        "uncertain_match": (
            "{stamp} This person looks like {name} ({score}), but you are not sure. "
            "Ask before using that name."
        ),
    },
    "fr": {
        "perception_stamp": "[Perception %H:%M]",
        "just_now": "a l'instant",
        "minutes_ago": "il y a {n} minutes",
        "hours_ago": "il y a {n} heures",
        "days_ago": "il y a {n} jours",
        "arrival_known": "{stamp} {name} vient d'arriver{when}.",
        "arrival_when": " (vu {ago})",
        "arrival_unknown": "{stamp} Quelqu'un que tu ne connais pas vient d'arriver.",
        "greet_known": "Salue {name} chaleureusement, en une phrase courte.",
        "greet_unknown": (
            "Salue cette personne que tu n'as jamais rencontree, en une phrase courte, "
            "et demande-lui son prenom."
        ),
        "uncertain_match": (
            "{stamp} Cette personne ressemble a {name} ({score}), mais tu n'en es pas sur. "
            "Demande avant d'utiliser ce prenom."
        ),
    },
}


def phrases(language: str | None) -> dict[str, str]:
    """The template table for ``language``, falling back to English.

    Matches on the base tag, so ``fr-FR`` and ``fr`` share a table.
    """
    if not language:
        return PHRASES[DEFAULT_LANGUAGE]
    tag = language.lower().replace("_", "-").split("-", 1)[0]
    return PHRASES.get(tag, PHRASES[DEFAULT_LANGUAGE])
