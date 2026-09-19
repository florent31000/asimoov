"""Persona contract (persona.v1) and its YAML loader.

Extends the shape of Neon's `config/personality.yaml`. See
``schemas/persona.v1.json`` and ``examples/persona.neon.yaml``. `pyyaml` is
used here (it is a core dependency of the whole distribution), but every
dataclass in this module stays plain stdlib so `Persona` objects can be
built and compared without touching YAML at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from asimoov.contracts.vocab import EMOTIONS, is_emotion

INITIATIVE_LEVELS: tuple[str, ...] = ("low", "medium", "high")


class PersonaError(ValueError):
    """Raised when a persona YAML file is missing or malformed.

    The message always includes the offending file path (or dict context)
    and the specific field at fault, so a bad persona fails loudly instead
    of falling back to defaults.
    """


@dataclass(frozen=True)
class Relationships:
    """How the persona relates to owners vs. strangers."""

    owners: tuple[str, ...] = ()
    owners_style: str = ""
    strangers_style: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "owners": list(self.owners),
            "owners_style": self.owners_style,
            "strangers_style": self.strangers_style,
        }


@dataclass(frozen=True)
class Initiative:
    """How proactive the persona is (greeting, cooldowns, quiet hours)."""

    level: str = "medium"
    greet_on_arrival: bool = True
    cooldown_s: int = 120
    quiet_hours: tuple[str, str] | None = None

    def __post_init__(self) -> None:
        if self.level not in INITIATIVE_LEVELS:
            raise PersonaError(f"initiative.level must be one of {INITIATIVE_LEVELS}, got {self.level!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "greet_on_arrival": self.greet_on_arrival,
            "cooldown_s": self.cooldown_s,
            "quiet_hours": list(self.quiet_hours) if self.quiet_hours else None,
        }


@dataclass(frozen=True)
class VoiceConfig:
    """Which voice provider/model/voice this persona speaks with."""

    provider: str
    model: str
    voice: str
    transcription_language: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "voice": self.voice,
            "transcription_language": self.transcription_language,
        }


@dataclass(frozen=True)
class Persona:
    """A robot's character: identity, style, relationships, voice, rules.

    Raises:
        PersonaError: if any entry of ``emotions`` is not in
            ``vocab.EMOTIONS``, or if ``initiative.level`` is invalid.
    """

    name: str
    language: str
    identity: str
    traits: tuple[str, ...] = ()
    speaking_style: str = ""
    relationships: Relationships = field(default_factory=Relationships)
    emotions: tuple[str, ...] = EMOTIONS
    initiative: Initiative = field(default_factory=Initiative)
    rules: tuple[str, ...] = ()
    voice: VoiceConfig | None = None
    fragments: tuple[str, ...] = ()
    api_version: str = "asimoov/v1"

    def __post_init__(self) -> None:
        for emotion in self.emotions:
            if not is_emotion(emotion):
                raise PersonaError(f"unknown emotion in persona.emotions: {emotion!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "apiVersion": self.api_version,
            "kind": "Persona",
            "name": self.name,
            "language": self.language,
            "identity": self.identity,
            "traits": list(self.traits),
            "speaking_style": self.speaking_style,
            "relationships": self.relationships.to_dict(),
            "emotions": list(self.emotions),
            "initiative": self.initiative.to_dict(),
            "rules": list(self.rules),
            "voice": self.voice.to_dict() if self.voice else None,
            "fragments": list(self.fragments),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Persona:
        relationships_raw = payload.get("relationships", {})
        initiative_raw = payload.get("initiative", {})
        voice_raw = payload.get("voice")
        quiet_hours = initiative_raw.get("quiet_hours")
        return cls(
            name=payload["name"],
            language=payload["language"],
            identity=payload["identity"],
            traits=tuple(payload.get("traits", [])),
            speaking_style=payload.get("speaking_style", ""),
            relationships=Relationships(
                owners=tuple(relationships_raw.get("owners", [])),
                owners_style=relationships_raw.get("owners_style", ""),
                strangers_style=relationships_raw.get("strangers_style", ""),
            ),
            emotions=tuple(payload.get("emotions", EMOTIONS)),
            initiative=Initiative(
                level=initiative_raw.get("level", "medium"),
                greet_on_arrival=initiative_raw.get("greet_on_arrival", True),
                cooldown_s=initiative_raw.get("cooldown_s", 120),
                quiet_hours=tuple(quiet_hours) if quiet_hours else None,
            ),
            rules=tuple(payload.get("rules", [])),
            voice=(
                VoiceConfig(
                    provider=voice_raw["provider"],
                    model=voice_raw["model"],
                    voice=voice_raw["voice"],
                    transcription_language=voice_raw.get("transcription_language", payload.get("language", "en")),
                )
                if voice_raw
                else None
            ),
            fragments=tuple(payload.get("fragments", [])),
            api_version=payload.get("apiVersion", "asimoov/v1"),
        )


_REQUIRED_FIELDS: tuple[str, ...] = ("name", "language", "identity")


def load_persona(path: str | Path) -> Persona:
    """Load and validate a persona YAML file.

    Raises:
        PersonaError: if the file cannot be parsed as a YAML mapping, if a
            required field (name, language, identity) is missing, or if a
            nested field (voice, initiative, emotions) is malformed. Every
            message names ``path`` and the offending field.
    """
    path = Path(path)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PersonaError(f"{path}: cannot read persona file ({exc})") from exc

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise PersonaError(f"{path}: invalid YAML ({exc})") from exc

    if not isinstance(data, dict):
        raise PersonaError(f"{path}: expected a YAML mapping at the top level, got {type(data).__name__}")

    missing = [field_name for field_name in _REQUIRED_FIELDS if field_name not in data]
    if missing:
        raise PersonaError(f"{path}: missing required field(s): {', '.join(missing)}")

    try:
        return Persona.from_dict(data)
    except KeyError as exc:
        raise PersonaError(f"{path}: missing required field {exc}") from exc
    except (TypeError, ValueError) as exc:
        if isinstance(exc, PersonaError):
            raise PersonaError(f"{path}: {exc}") from exc
        raise PersonaError(f"{path}: {exc}") from exc
