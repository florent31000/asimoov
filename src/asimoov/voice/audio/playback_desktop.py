"""Desktop speaker playback (`sounddevice`) with a real playback head.

The output stream is pull-based: its callback reads from
`StreamPlaybackTracker`, which is therefore the single source of truth for
`remaining_ms`, `played_ms` and `energy_at_head`, hardware latency included.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from asimoov.contracts.audio import PCM_CHANNELS, AudioSink, PlaybackTracker
from asimoov.voice.audio.tracker import StreamPlaybackTracker

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover - depends on the host install
    sd = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


def available() -> bool:
    """True when `sounddevice` is importable (reported by ``asimoov doctor``)."""
    return sd is not None


class DesktopAudioSink(AudioSink):
    """PortAudio output stream fed by a `StreamPlaybackTracker`."""

    def __init__(
        self,
        sample_rate_hz: int = 24000,
        *,
        device: int | str | None = None,
        block_ms: float = 20.0,
        loop: asyncio.AbstractEventLoop | None = None,
        on_energy: Callable[[float], None] | None = None,
    ) -> None:
        self.sample_rate_hz = sample_rate_hz
        self._device = device
        self._block_ms = block_ms
        self._loop = loop
        self._tracker = StreamPlaybackTracker(sample_rate_hz)
        self._stream = None
        if on_energy is not None:
            self._tracker.set_energy_callback(on_energy, loop)

    async def play(self, item_id: str, pcm16: bytes) -> None:
        if self._stream is None:
            await self._open()
        self._tracker.feed(item_id, pcm16)

    async def stop(self) -> None:
        stream, self._stream = self._stream, None
        self._tracker.flush()
        if stream is not None:
            stream.stop()
            stream.close()

    def tracker(self) -> PlaybackTracker:
        return self._tracker

    async def _open(self) -> None:
        if sd is None:
            raise RuntimeError("sounddevice is not installed: pip install sounddevice")
        self._loop = self._loop or asyncio.get_running_loop()
        blocksize = max(1, int(self.sample_rate_hz * self._block_ms / 1000))
        stream = sd.RawOutputStream(
            samplerate=self.sample_rate_hz,
            channels=PCM_CHANNELS,
            dtype="int16",
            blocksize=blocksize,
            device=self._device,
            callback=self._callback,
        )
        stream.start()
        self._stream = stream
        self._tracker.set_loop(self._loop)
        latency_ms = float(stream.latency) * 1000.0
        self._tracker.set_output_latency_ms(latency_ms)
        log.info("output stream open (%d Hz, latency %.0f ms)", self.sample_rate_hz, latency_ms)

    def _callback(self, outdata, frames, time_info, status) -> None:
        if status:
            log.debug("output stream status: %s", status)
        outdata[:] = self._tracker.read(frames)
