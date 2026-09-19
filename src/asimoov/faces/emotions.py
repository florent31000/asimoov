"""Shared emotion table (``web/emotions.json``), read by the Kivy renderer.

The same file is fetched by ``face.js`` so the web canvas and the Kivy
widget draw the same eyes. Ported from ``neon/src/ui/eyes.py``
(``EYE_PARAMS``), plus a light-theme iris per emotion and the two theme
palettes.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

WEB_DIR = Path(__file__).parent / "web"
EMOTIONS_PATH = WEB_DIR / "emotions.json"

EMOTION_KEYS: tuple[str, ...] = ("pupil", "lid_top", "lid_bot", "brow", "px", "py", "iris")
THEMES: tuple[str, ...] = ("dark", "light")


@lru_cache(maxsize=1)
def load_emotion_table() -> dict[str, Any]:
    """Return the parsed ``emotions.json`` (cached)."""
    return json.loads(EMOTIONS_PATH.read_text(encoding="utf-8"))


def emotion_params(emotion: str) -> dict[str, Any]:
    """Return the drawing parameters for ``emotion``.

    Raises:
        KeyError: if ``emotion`` is not in the table.
    """
    return load_emotion_table()["emotions"][emotion]


def theme_palette(theme: str) -> dict[str, Any]:
    """Return the palette for ``theme`` (``dark`` or ``light``).

    Raises:
        KeyError: if ``theme`` is unknown.
    """
    return load_emotion_table()["themes"][theme]


def hex_to_rgb(value: str) -> tuple[float, float, float]:
    """Convert ``#RRGGBB`` to a 0..1 RGB triple."""
    raw = value.lstrip("#")
    if len(raw) != 6:
        raise ValueError(f"expected #RRGGBB, got {value!r}")
    return tuple(int(raw[i : i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]
