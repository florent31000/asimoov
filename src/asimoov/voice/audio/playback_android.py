"""Android speaker playback (jnius / `AudioTrack`) with a real playback head.

`AndroidPlaybackTracker` reads ``AudioTrack.getPlaybackHeadPosition()``, the
frame the DAC is actually on -- what Neon never looked at. The track is built
with ``USAGE_VOICE_COMMUNICATION`` so the capture-side `AcousticEchoCanceler`
has a reference signal to cancel.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections.abc import Callable

from asimoov.contracts.audio import AudioSink, PlaybackTracker
from asimoov.voice.audio.tracker import HeadTracker

try:
    from jnius import autoclass
except ImportError:  # pragma: no cover - Android only
    autoclass = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

_UINT32 = 0xFFFFFFFF


def available() -> bool:
    """True when running under pyjnius (reported by ``asimoov doctor``)."""
    return autoclass is not None


class AndroidPlaybackTracker(HeadTracker):
    """Playback head read from ``AudioTrack.getPlaybackHeadPosition()``."""

    def __init__(self, sample_rate_hz: int, track_provider: Callable[[], object]) -> None:
        super().__init__(sample_rate_hz)
        self._track_provider = track_provider

    def _head_frames(self) -> int:
        track = self._track_provider()
        if track is None:
            return 0
        # The Java call returns a signed int wrapping a uint32 frame counter.
        return track.getPlaybackHeadPosition() & _UINT32

    def _reset_device(self) -> None:
        track = self._track_provider()
        if track is None:
            return
        track.pause()
        track.flush()  # resets getPlaybackHeadPosition() to 0
        track.play()


class AndroidAudioSink(AudioSink):
    """`AudioTrack` writer thread; `tracker` follows the hardware head."""

    def __init__(
        self,
        sample_rate_hz: int = 24000,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        on_energy: Callable[[float], None] | None = None,
    ) -> None:
        self.sample_rate_hz = sample_rate_hz
        self._loop = loop
        self._track = None
        self._tracker = AndroidPlaybackTracker(sample_rate_hz, lambda: self._track)
        if on_energy is not None:
            self._tracker.set_energy_callback(on_energy, loop)
        self._queue: queue.Queue[tuple[str, bytes] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = False

    async def play(self, item_id: str, pcm16: bytes) -> None:
        if autoclass is None:
            raise RuntimeError("pyjnius is not available: Android playback needs the [kivy] extra")
        if not self._running:
            self._loop = self._loop or asyncio.get_running_loop()
            self._tracker.set_loop(self._loop)
            self._running = True
            self._thread = threading.Thread(
                target=self._writer_loop, name="audio.playback", daemon=True
            )
            self._thread.start()
        self._queue.put((item_id, pcm16))

    async def stop(self) -> None:
        self._running = False
        self._queue.put(None)
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)
        track, self._track = self._track, None
        if track is not None:
            track.stop()
            track.release()

    def tracker(self) -> PlaybackTracker:
        return self._tracker

    def _writer_loop(self) -> None:
        self._track = self._build_track()
        self._track.play()
        while self._running:
            entry = self._queue.get()
            if entry is None:
                break
            item_id, pcm16 = entry
            written = self._track.write(bytearray(pcm16), 0, len(pcm16))
            if written <= 0:
                log.warning("AudioTrack.write returned %d", written)
                continue
            self._tracker.note_written(item_id, pcm16[:written])
            self._tracker.notify_head_moved()

    def _build_track(self):
        AudioTrack = autoclass("android.media.AudioTrack")
        AudioFormat = autoclass("android.media.AudioFormat")
        AudioAttributes = autoclass("android.media.AudioAttributes")

        min_buf = AudioTrack.getMinBufferSize(
            self.sample_rate_hz,
            AudioFormat.CHANNEL_OUT_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        attributes = (
            AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
            .build()
        )
        audio_format = (
            AudioFormat.Builder()
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .setSampleRate(self.sample_rate_hz)
            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
            .build()
        )
        return (
            AudioTrack.Builder()
            .setAudioAttributes(attributes)
            .setAudioFormat(audio_format)
            .setBufferSizeInBytes(max(min_buf * 2, 8192))
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        )
