"""Android microphone capture (jnius / `AudioRecord`).

Two deliberate differences with Neon (plan.md section 2, item 1): the source is
``VOICE_COMMUNICATION`` instead of ``MIC``, and the platform
``AcousticEchoCanceler`` is attached to the record session. No home-made
spectral gate: Neon's filter fought the platform AEC instead of using it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable

from asimoov.contracts.audio import PCM_SAMPLE_WIDTH_BYTES, AudioSource
from asimoov.voice.audio.resample import resample_pcm16

try:
    from jnius import autoclass
except ImportError:  # pragma: no cover - Android only
    autoclass = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

FALLBACK_RATE_HZ = 16000


def available() -> bool:
    """True when running under pyjnius (reported by ``asimoov doctor``)."""
    return autoclass is not None


class AndroidAudioSource(AudioSource):
    """`AudioRecord` capture thread delivering PCM16 mono on the event loop."""

    def __init__(
        self,
        sample_rate_hz: int = 24000,
        *,
        chunk_ms: float = 20.0,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.sample_rate_hz = sample_rate_hz
        self._chunk_ms = chunk_ms
        self._loop = loop
        self._device_rate = sample_rate_hz
        self._on_chunk: Callable[[bytes], None] | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._aec = None
        self.aec_enabled = False

    async def start(self, on_chunk: Callable[[bytes], None]) -> None:
        if autoclass is None:
            raise RuntimeError("pyjnius is not available: Android capture needs the [kivy] extra")
        if self._running:
            return
        self._on_chunk = on_chunk
        self._loop = self._loop or asyncio.get_running_loop()
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, name="audio.capture", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._running = False
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)

    def _capture_loop(self) -> None:
        AudioRecord = autoclass("android.media.AudioRecord")
        AudioFormat = autoclass("android.media.AudioFormat")
        MediaAudioSource = autoclass("android.media.MediaRecorder$AudioSource")
        AcousticEchoCanceler = autoclass("android.media.audiofx.AcousticEchoCanceler")

        channel = AudioFormat.CHANNEL_IN_MONO
        encoding = AudioFormat.ENCODING_PCM_16BIT
        rate = self.sample_rate_hz
        min_buf = AudioRecord.getMinBufferSize(rate, channel, encoding)
        if min_buf <= 0:
            log.warning("AudioRecord refuses %d Hz, capturing at %d Hz", rate, FALLBACK_RATE_HZ)
            rate = FALLBACK_RATE_HZ
            min_buf = AudioRecord.getMinBufferSize(rate, channel, encoding)
        self._device_rate = rate

        chunk_bytes = int(rate * self._chunk_ms / 1000) * PCM_SAMPLE_WIDTH_BYTES
        recorder = AudioRecord(
            MediaAudioSource.VOICE_COMMUNICATION,
            rate,
            channel,
            encoding,
            max(min_buf * 2, chunk_bytes * 4),
        )
        session_id = recorder.getAudioSessionId()
        if AcousticEchoCanceler.isAvailable():
            self._aec = AcousticEchoCanceler.create(session_id)
            if self._aec is not None:
                self._aec.setEnabled(True)
                self.aec_enabled = True
        if not self.aec_enabled:
            log.warning("no platform AcousticEchoCanceler: barge-in runs half-duplex")

        recorder.startRecording()
        log.info("AudioRecord started (%d Hz, aec=%s)", rate, self.aec_enabled)
        try:
            while self._running:
                buf = bytearray(chunk_bytes)
                read = recorder.read(buf, 0, len(buf))
                if read <= 0:
                    continue
                pcm = bytes(buf[:read])
                if rate != self.sample_rate_hz:
                    pcm = resample_pcm16(pcm, rate, self.sample_rate_hz)
                callback, loop = self._on_chunk, self._loop
                if callback is not None and loop is not None:
                    loop.call_soon_threadsafe(callback, pcm)
        finally:
            recorder.stop()
            recorder.release()
            if self._aec is not None:
                self._aec.release()
                self._aec = None
                self.aec_enabled = False
