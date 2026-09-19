"""The persona loader must accept the frozen example and reject bad files clearly."""

from __future__ import annotations

from pathlib import Path

import pytest

from asimoov.contracts.persona import PersonaError, load_persona

EXAMPLES_DIR = Path(__file__).parents[2] / "src" / "asimoov" / "contracts" / "examples"


def test_load_persona_accepts_the_frozen_example() -> None:
    persona = load_persona(EXAMPLES_DIR / "persona.neon.yaml")
    assert persona.name == "Néon"
    assert persona.language == "fr"
    assert persona.relationships.owners == ("Caroline", "Sam", "Florent")


def test_load_persona_rejects_missing_required_field(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad_persona.yaml"
    bad_file.write_text("language: fr\nidentity: 'Tu es {name}.'\n", encoding="utf-8")

    with pytest.raises(PersonaError) as exc_info:
        load_persona(bad_file)

    message = str(exc_info.value)
    assert str(bad_file) in message
    assert "name" in message


def test_load_persona_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad_persona_list.yaml"
    bad_file.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    with pytest.raises(PersonaError, match="expected a YAML mapping"):
        load_persona(bad_file)


def test_load_persona_rejects_unknown_emotion(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad_persona_emotion.yaml"
    bad_file.write_text(
        "name: Test\nlanguage: en\nidentity: 'You are {name}.'\nemotions: [neutral, furious]\n",
        encoding="utf-8",
    )

    with pytest.raises(PersonaError, match="furious"):
        load_persona(bad_file)


def test_load_persona_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PersonaError, match="cannot read"):
        load_persona(tmp_path / "does_not_exist.yaml")
