"""`KivyFace`: the same procedural eyes as the web face, drawn by Kivy.

Used by the Android app (plan.md section 4.10) and runnable on a desktop
with ``python -m asimoov.faces.kivy.app --demo``. Kivy is an optional extra
(``pip install asimoov[kivy]``): this module imports without it, and only
`KivyFace` and `EyesWidget` need it at runtime.

`render` is called from the asyncio loop and only puts the state in a
`queue.Queue`; the Kivy clock drains it at 30 Hz on the UI thread, so no
drawing ever happens off the UI thread and a slow frame drops states
instead of queuing them.
"""

from __future__ import annotations

import math
import queue
import time
from typing import Any

from asimoov.contracts.face import FaceRenderer, FaceState
from asimoov.faces.emotions import hex_to_rgb, load_emotion_table

try:
    from kivy.clock import Clock
    from kivy.graphics import Color, Ellipse, Line, Rectangle
    from kivy.uix.widget import Widget
except ImportError as exc:  # optional extra, see module docstring
    _IMPORT_ERROR: ImportError | None = exc
    Widget = object  # type: ignore[assignment, misc]
else:
    _IMPORT_ERROR = None

DEFAULT_FPS = 30.0
QUEUE_SIZE = 4
_NUMERIC_KEYS = ("pupil", "lid_top", "lid_bot", "brow", "px", "py")


def require_kivy() -> None:
    """Raise a clear error when Kivy is missing.

    Raises:
        ImportError: if Kivy is not installed.
    """
    if _IMPORT_ERROR is not None:
        raise ImportError("KivyFace needs Kivy: pip install 'asimoov[kivy]'") from _IMPORT_ERROR


def _params_for(emotion: str, intensity: float, theme: str) -> dict[str, Any]:
    table = load_emotion_table()["emotions"]
    entry = table.get(emotion, table["neutral"])
    neutral = table["neutral"]
    t = min(max(intensity, 0.0), 1.0)
    params: dict[str, Any] = {
        key: neutral[key] + (entry[key] - neutral[key]) * t for key in _NUMERIC_KEYS
    }
    iris = hex_to_rgb(entry["iris"][theme])
    neutral_iris = hex_to_rgb(neutral["iris"][theme])
    params["iris"] = tuple(n + (c - n) * t for n, c in zip(neutral_iris, iris, strict=True))
    return params


def _mix(a: dict[str, Any], b: dict[str, Any], t: float) -> dict[str, Any]:
    mixed: dict[str, Any] = {key: a[key] + (b[key] - a[key]) * t for key in _NUMERIC_KEYS}
    mixed["iris"] = tuple(x + (y - x) * t for x, y in zip(a["iris"], b["iris"], strict=True))
    return mixed


class EyesWidget(Widget):
    """Draws the eyes and the mouth bar from the shared emotion table."""

    def __init__(self, *, theme: str = "dark", **kwargs: Any) -> None:
        require_kivy()
        super().__init__(**kwargs)
        table = load_emotion_table()
        self._timing = table["timing"]
        self._palette = table["themes"][theme]
        self._theme = theme
        self._state = FaceState(emotion="neutral")
        self._from = _params_for("neutral", 1.0, theme)
        self._to = self._from
        self._current = dict(self._from)
        self._transition_started = time.monotonic()
        self._gaze = [0.0, 0.0]
        self._lip = 0.0
        self._blink_started = -1e9
        self._last_blink_flag = False
        self.bind(pos=lambda *_: self.draw(), size=lambda *_: self.draw())

    def apply_state(self, state: FaceState) -> None:
        changed = (
            state.emotion != self._state.emotion
            or abs(state.intensity - self._state.intensity) > 0.01
        )
        if changed:
            self._from = dict(self._current)
            self._to = _params_for(state.emotion, state.intensity, self._theme)
            self._transition_started = time.monotonic()
        if state.blink and not self._last_blink_flag:
            self._blink_started = time.monotonic()
        self._last_blink_flag = state.blink
        self._state = state

    def tick(self, dt: float) -> None:
        now = time.monotonic()
        t = min(1.0, (now - self._transition_started) / (self._timing["transition_ms"] / 1000))
        self._current = _mix(self._from, self._to, t * t * (3 - 2 * t))
        k_gaze = 1 - math.exp(-dt / 0.08)
        self._gaze[0] += (self._state.gaze.x - self._gaze[0]) * k_gaze
        self._gaze[1] += (self._state.gaze.y - self._gaze[1]) * k_gaze
        k_lip = 1 - math.exp(-dt / (self._timing["lip_smoothing_ms"] / 1000))
        self._lip += (self._state.lip - self._lip) * k_lip
        self.draw()

    def _blink_closure(self) -> float:
        t = (time.monotonic() - self._blink_started) / (self._timing["blink_ms"] / 1000)
        return math.sin(math.pi * t) if 0.0 <= t <= 1.0 else 0.0

    def draw(self) -> None:
        self.canvas.clear()
        params = self._current
        bg = hex_to_rgb(self._palette["bg"])
        eye_color = hex_to_rgb(self._palette["eye"])
        iris = params["iris"]
        glow = self._palette["glow"] * (1.25 if self._state.talking else 1.0)

        w, h = self.size
        x0, y0 = self.pos
        unit = min(w, h)
        eye_w, eye_h = unit * 0.16, unit * 0.22
        spacing = unit * 0.26
        cy = y0 + h * 0.56
        lid_top = min(1.0, max(params["lid_top"] + self._state.eyelids * 0.8, self._blink_closure() * 0.98))
        lid_bot = min(1.0, max(0.0, params["lid_bot"]))
        pupil_r = eye_w * 0.42 * params["pupil"]

        with self.canvas:
            Color(*bg, 1)
            Rectangle(pos=self.pos, size=self.size)
            for side in (-1, 1):
                cx = x0 + w / 2 + side * spacing
                Color(*eye_color, 1)
                Ellipse(pos=(cx - eye_w, cy - eye_h), size=(eye_w * 2, eye_h * 2))

                px = cx + (params["px"] + self._gaze[0]) * eye_w * 0.55
                py = cy + (params["py"] + self._gaze[1]) * eye_h * 0.5
                if pupil_r > 0.5:
                    Color(*iris, 0.35 * glow)
                    Ellipse(pos=(px - pupil_r * 1.8, py - pupil_r * 1.8), size=(pupil_r * 3.6, pupil_r * 3.6))
                    Color(*iris, 1)
                    Ellipse(pos=(px - pupil_r, py - pupil_r), size=(pupil_r * 2, pupil_r * 2))
                    Color(1, 1, 1, 0.75)
                    hl = pupil_r * 0.25
                    Ellipse(pos=(px + pupil_r * 0.3 - hl, py + pupil_r * 0.3 - hl), size=(hl * 2, hl * 2))

                Color(*bg, 1)
                if lid_top > 0.005:
                    lid_h = eye_h * 2 * lid_top
                    Rectangle(pos=(cx - eye_w - 2, cy + eye_h - lid_h), size=(eye_w * 2 + 4, lid_h + 2))
                if lid_bot > 0.005:
                    lid_h = eye_h * 2 * lid_bot
                    Rectangle(pos=(cx - eye_w - 2, cy - eye_h - 2), size=(eye_w * 2 + 4, lid_h + 2))

                angle = math.radians(params["brow"] * side)
                brow_y = cy + eye_h * 1.45
                dx, dy = eye_w * 0.9 * math.cos(angle), eye_w * 0.9 * math.sin(angle)
                Color(*iris, 0.85)
                Line(
                    points=[cx - dx, brow_y - dy, cx + dx, brow_y + dy],
                    width=max(2.0, eye_h * 0.06),
                    cap="round",
                )

            mouth_w = unit * 0.22
            mouth_h = max(unit * 0.005, self._lip * unit * 0.085)
            Color(*hex_to_rgb(self._palette["mouth"]), 1)
            Rectangle(
                pos=(x0 + w / 2 - mouth_w / 2, cy - unit * 0.42 - mouth_h / 2),
                size=(mouth_w, mouth_h),
            )


class KivyFace(FaceRenderer):
    """`FaceRenderer` drawing on an `EyesWidget` through a 30 Hz Kivy clock."""

    def __init__(self, *, theme: str = "dark", fps: float = DEFAULT_FPS) -> None:
        require_kivy()
        self.widget = EyesWidget(theme=theme)
        self._fps = fps
        self._queue: queue.Queue[FaceState] = queue.Queue(maxsize=QUEUE_SIZE)
        self._event: Any = None
        self.dropped_count = 0

    async def start(self, ctx: dict[str, Any]) -> None:
        if self._event is None:
            self._event = Clock.schedule_interval(self._on_tick, 1.0 / self._fps)

    async def render(self, state: FaceState) -> None:
        try:
            self._queue.put_nowait(state)
        except queue.Full:
            self._drop_oldest()
            self._queue.put_nowait(state)

    async def stop(self) -> None:
        if self._event is not None:
            self._event.cancel()
            self._event = None

    def _drop_oldest(self) -> None:
        try:
            self._queue.get_nowait()
            self.dropped_count += 1
        except queue.Empty:
            pass

    def _on_tick(self, dt: float) -> None:
        latest: FaceState | None = None
        while True:
            try:
                latest = self._queue.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            self.widget.apply_state(latest)
        self.widget.tick(dt)
