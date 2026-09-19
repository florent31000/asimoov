"""The Kivy renderer imports without Kivy and shares the web emotion table."""

from __future__ import annotations

import pytest

from asimoov.faces import emotions
from asimoov.faces.kivy import renderer


def test_importing_the_module_never_needs_the_optional_extra() -> None:
    assert renderer.DEFAULT_FPS == 30.0


def test_instantiating_without_kivy_says_which_extra_to_install() -> None:
    if renderer._IMPORT_ERROR is None:
        pytest.skip("Kivy is installed in this environment")
    with pytest.raises(ImportError, match=r"asimoov\[kivy\]"):
        renderer.KivyFace()


def test_the_kivy_parameters_come_from_the_shared_table() -> None:
    table = emotions.load_emotion_table()["emotions"]
    params = renderer._params_for("angry", 1.0, "dark")
    assert params["pupil"] == table["angry"]["pupil"]
    assert params["brow"] == table["angry"]["brow"]
    assert params["iris"] == pytest.approx(emotions.hex_to_rgb(table["angry"]["iris"]["dark"]))


def test_intensity_zero_falls_back_to_the_neutral_face() -> None:
    neutral = renderer._params_for("neutral", 1.0, "dark")
    faded = renderer._params_for("angry", 0.0, "dark")
    assert faded == pytest.approx(neutral)
