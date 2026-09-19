"""The emotion table shared by face.js and the Kivy renderer."""

from __future__ import annotations

import re

import pytest

from asimoov.contracts.vocab import EMOTIONS
from asimoov.faces.emotions import (
    EMOTION_KEYS,
    THEMES,
    WEB_DIR,
    hex_to_rgb,
    load_emotion_table,
)

TABLE = load_emotion_table()
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def test_the_table_covers_exactly_the_nine_persona_emotions() -> None:
    assert tuple(TABLE["emotions"]) == EMOTIONS


@pytest.mark.parametrize("emotion", EMOTIONS)
def test_every_emotion_has_every_parameter_in_range(emotion: str) -> None:
    params = TABLE["emotions"][emotion]
    assert set(params) == set(EMOTION_KEYS)
    assert 0.0 <= params["pupil"] <= 2.0
    assert 0.0 <= params["lid_top"] <= 1.0
    assert 0.0 <= params["lid_bot"] <= 1.0
    assert -45 <= params["brow"] <= 45
    assert -1.0 <= params["px"] <= 1.0
    assert -1.0 <= params["py"] <= 1.0
    for theme in THEMES:
        assert HEX.match(params["iris"][theme]), f"{emotion}/{theme} must be #RRGGBB"


@pytest.mark.parametrize("theme", THEMES)
def test_every_theme_has_a_full_palette(theme: str) -> None:
    palette = TABLE["themes"][theme]
    assert set(palette) == {"bg", "eye", "stroke", "stroke_width", "mouth", "glow", "hud"}
    for key in ("bg", "eye", "stroke", "mouth", "hud"):
        assert HEX.match(palette[key]), f"{theme}.{key} must be #RRGGBB"


def test_the_light_theme_stays_readable_on_the_cream_background() -> None:
    """The site hero draws the eyes on cream: the iris must stay dark there."""
    for emotion in EMOTIONS:
        r, g, b = hex_to_rgb(TABLE["emotions"][emotion]["iris"]["light"])
        luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
        assert luminance < 0.55, f"{emotion} light iris is too pale on cream"


def test_timing_matches_the_plan() -> None:
    assert TABLE["timing"] == {
        "transition_ms": 300,
        "blink_ms": 150,
        "lip_smoothing_ms": 50,
    }


def test_face_js_has_no_build_step_and_no_dependency() -> None:
    source = (WEB_DIR / "face.js").read_text(encoding="utf-8")
    assert "import " not in source
    assert "require(" not in source
    assert "//cdn" not in source and "https://" not in source


def test_the_page_only_references_its_own_local_assets() -> None:
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'href="face.css"' in html
    assert 'src="face.js"' in html
    assert "http://" not in html and "https://" not in html
