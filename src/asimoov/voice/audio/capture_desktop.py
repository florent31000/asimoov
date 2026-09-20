"""Desktop microphone capture (`sounddevice`).

`sounddevice` is not a core dependency: the import is guarded, and `available`
tells `doctor` whether desktop audio can run at all.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from asimoov.contracts.audio import PCM_CHANNELS, PCM_SAMPLE_WIDTH_BYTES, AudioSource
from asimoov.voice.audio.resample import resample_pcm16

try:
    import sounddevice as sd
except (ImportError, OSError):  # pragma: no cover - missing package or PortAudio library
    sd = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


def available() -> bool:
    """True when `sounddevice` is importable (reported by ``asimoov doctor``)."""
    return sd is not None


class DesktopAudioSource(AudioSource):
    """PortAudio input stream delivering PCM16 mono chunks on the event loop.

    If the device refuses ``sample_rate_hz``, the stream is opened at the
    device's default rate and each chunk is resampled, so ``sample_rate_hz``
    stays the rate the caller asked for.
    """

    def __init__(
        self,
        sample_rate_hz: int = 24000,
        *,
        device: int | str | None = None,
        chunk_ms: float = 20.0,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.sample_rate_hz = sample_rate_hz
        self._device = device
        self._chunk_ms = chunk_ms
        self._loop = loop
        self._device_rate = sample_rate_hz
        self._stream = None
        self._on_chunk: Callable[[bytes], None] | None = None

    async def start(self, on_chunk: Callable[[bytes], None]) -> None:
        if sd is None:
            raise RuntimeError("sounddevice is not installed: pip install sounddevice")
        if self._stream is not None:
            return
        self._on_chunk = on_chunk
        self._loop = self._loop or asyncio.get_running_loop()
        self._device_rate = self.sample_rate_hz
        try:
            self._stream = self._open(self.sample_rate_hz)
        except Exception as exc:
            fallback = int(sd.query_devices(self._device, "input")["default_samplerate"])
            if fallback == self.sample_rate_hz:
                raise RuntimeError(f"cannot open microphone at {self.sample_rate_hz} Hz") from exc
            log.warning(
                "microphone refused %d Hz (%s), capturing at %d Hz and resampling",
                self.sample_rate_hz,
                exc,
                fallback,
            )
            self._device_rate = fallback
            self._stream = self._open(fallback)
        self._stream.start()

    async def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()
            stream.close()

    def _open(self, rate: int):
        blocksize = max(1, int(rate * self._chunk_ms / 1000))
        return sd.RawInputStream(
            samplerate=rate,
            channels=PCM_CHANNELS,
            dtype="int16",
            blocksize=blocksize,
            device=self._device,
            callback=self._callback,
        )

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            log.debug("input stream status: %s", status)
        pcm = bytes(indata[: frames * PCM_SAMPLE_WIDTH_BYTES * PCM_CHANNELS])
        if self._device_rate != self.sample_rate_hz:
            pcm = resample_pcm16(pcm, self._device_rate, self.sample_rate_hz)
        callback, loop = self._on_chunk, self._loop
        if callback is not None and loop is not None:
            loop.call_soon_threadsafe(callback, pcm)
