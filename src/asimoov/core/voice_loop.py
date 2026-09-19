"""The audio loop the runtime owns: microphone in, speaker out, barge-in.

`voice/` supplies the pieces (`MicGate`, `BargeInController`,
`SessionManager`, the devices); nobody was wiring them, so no ASIMOOV
process ever opened a microphone (review, blocker 2). This module is that
wiring and nothing else: it resolves which devices to open from the body
and ``robot.yaml: audio``, and runs the loop `docs/voice.md` describes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from asimoov.contracts.audio import AudioSink, AudioSource
from asimoov.voice.audio.tracker import DEFAULT_TAIL_MS, MicGate

if TYPE_CHECKING:  # `barge_in` pulls numpy and, with the [vad] extra,
    # onnxruntime and its native libraries: it is imported when a loop is
    # built, not when the core is imported.
    from asimoov.voice.barge_in import BargeInConfig

log = logging.getLogger(__name__)

BACKENDS = ("auto", "body", "desktop", "android", "none")


class AudioError(RuntimeError):
    """Raised when the configured audio backend cannot be opened."""


@dataclass(frozen=True)
class AudioPlan:
    """Which backend will serve the microphone and the speaker, and why.

    ``ready`` is False when neither the body nor the host can open a
    device: the robot would then be deaf and mute, which ``asimoov doctor``
    must say out loud instead of reporting a healthy robot.
    """

    source: str
    sink: str
    reason: str | None = None

    @property
    def ready(self) -> bool:
        return self.source != "none" and self.sink != "none"

    def describe(self) -> str:
        text = f"in={self.source} out={self.sink}"
        return f"{text} ({self.reason})" if self.reason else text


def _desktop_available() -> bool:
    from asimoov.voice.audio import capture_desktop, playback_desktop

    return capture_desktop.available() and playback_desktop.available()


def _android_available() -> bool:
    from asimoov.voice.audio import capture_android, playback_android

    return capture_android.available() and playback_android.available()


def plan_audio(audio_config: dict[str, Any], body: Any) -> AudioPlan:
    """Decide which backend serves audio, without opening anything.

    ``robot.yaml: audio.backend`` is ``auto`` (the default), ``body``,
    ``desktop``, ``android`` or ``none``. ``auto`` prefers what the body
    offers, then the host.
    """
    backend = str(audio_config.get("backend", "auto"))
    if backend not in BACKENDS:
        raise AudioError(f"unknown audio backend {backend!r}, expected one of {BACKENDS}")
    if backend == "none":
        return AudioPlan("none", "none", "disabled by robot.yaml: audio.backend")

    body_source = body.audio_source() is not None
    body_sink = body.audio_sink() is not None
    if backend == "body" or (backend == "auto" and body_source and body_sink):
        if body_source and body_sink:
            return AudioPlan("body", "body")
        if backend == "body":
            return AudioPlan("none", "none", "the body offers no audio device")

    if backend in ("auto", "desktop") and _desktop_available():
        return AudioPlan("desktop", "desktop")
    if backend in ("auto", "android") and _android_available():
        return AudioPlan("android", "android")

    missing = {
        "desktop": "sounddevice is not installed (pip install asimoov[desktop])",
        "android": "pyjnius is not available",
        "auto": "no audio backend on this host (pip install asimoov[desktop])",
    }[backend if backend in ("desktop", "android") else "auto"]
    return AudioPlan("none", "none", missing)


def open_audio(
    plan: AudioPlan,
    audio_config: dict[str, Any],
    body: Any,
    *,
    sample_rate_hz: int,
) -> tuple[AudioSource, AudioSink]:
    """Instantiate the devices ``plan`` names. Nothing is started yet.

    Raises:
        AudioError: if ``plan`` is not `AudioPlan.ready`.
    """
    if not plan.ready:
        raise AudioError(plan.reason or "no audio backend available")
    if plan.source == "body":
        source, sink = body.audio_source(), body.audio_sink()
        if source is None or sink is None:
            raise AudioError("the body stopped offering its audio devices")
        return source, sink
    if plan.source == "desktop":
        from asimoov.voice.audio.capture_desktop import DesktopAudioSource
        from asimoov.voice.audio.playback_desktop import DesktopAudioSink

        return (
            DesktopAudioSource(sample_rate_hz, device=_device(audio_config, "source")),
            DesktopAudioSink(sample_rate_hz, device=_device(audio_config, "sink")),
        )
    from asimoov.voice.audio.capture_android import AndroidAudioSource
    from asimoov.voice.audio.playback_android import AndroidAudioSink

    return AndroidAudioSource(sample_rate_hz), AndroidAudioSink(sample_rate_hz)


def _device(audio_config: dict[str, Any], key: str) -> int | str | None:
    value = audio_config.get(key)
    return None if value in (None, "", "default") else value


class VoiceLoop:
    """Mic chunks to the provider, provider audio to the speaker.

    The only place microphone audio is used: while the speaker is audible
    it goes to the barge-in detector, never to the provider; once the
    `MicGate` reopens (real playback head plus a 250 ms tail) it is
    forwarded. `docs/voice.md` describes the same sequence.
    """

    def __init__(
        self,
        provider: Any,
        source: AudioSource,
        sink: AudioSink,
        *,
        barge_in: BargeInConfig | None = None,
        on_barge_in: Any = None,
    ) -> None:
        from asimoov.voice.barge_in import BargeInConfig as _Config
        from asimoov.voice.barge_in import BargeInController

        self.provider = provider
        self.source = source
        self.sink = sink
        self.tracker = sink.tracker()
        config = barge_in or _Config()
        self.gate = MicGate(self.tracker, tail_ms=config.tail_ms)
        self.barge_in = BargeInController(
            provider,
            self.tracker,
            gate=self.gate,
            on_barge_in=on_barge_in,
        )
        self.forwarded_chunks = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._started = False

    def set_provider(self, provider: Any) -> None:
        """Follow a session renewal: the live provider changed."""
        self.provider = provider
        self.barge_in.set_provider(provider)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self.source.start(self._on_chunk)
        self._started = True

    async def stop(self) -> None:
        self._started = False
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        await self.source.stop()
        await self.sink.stop()

    def _on_chunk(self, pcm16: bytes) -> None:
        """Called on the event loop by the capture device, once per chunk."""
        if not self._started or self._loop is None:
            return
        task = self._loop.create_task(self.handle_chunk(pcm16))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def handle_chunk(self, pcm16: bytes) -> None:
        """One microphone chunk. Public so tests drive it without a device."""
        if self.tracker.is_playing():
            await self.barge_in.feed(pcm16)
            return
        if not self.gate.is_open():
            return
        self.forwarded_chunks += 1
        await self.provider.send_audio(pcm16)

    def is_silent(self) -> bool:
        """True when nothing is audible and no response is streaming."""
        if self.tracker.is_playing():
            return False
        return getattr(self.provider, "active_response_id", None) is None

    def capabilities(self) -> dict[str, Any]:
        return self.barge_in.capabilities()


__all__ = [
    "DEFAULT_TAIL_MS",
    "AudioError",
    "AudioPlan",
    "VoiceLoop",
    "open_audio",
    "plan_audio",
]
