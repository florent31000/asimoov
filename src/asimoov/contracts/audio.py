"""Audio I/O contracts: microphone source, speaker sink, playback tracking.

Not backed by a JSON Schema (audio never crosses the bus as JSON; PCM16
travels as binary `frame.*`-style messages or in-process callbacks). See
plan.md section 4.4 (barge-in and echo) for the timing rules these ABCs
exist to enforce.

PCM format, everywhere in ASIMOOV
---------------------------------
Every ``bytes`` payload named ``pcm16`` in these contracts (and in
`contracts.voice`: `VoiceProvider.send_audio`, `VoiceEvents.on_audio_out`)
is raw, headerless PCM:

- signed 16-bit integers (`PCM_SAMPLE_WIDTH_BYTES` = 2), little-endian
  (`PCM_BYTE_ORDER`), i.e. numpy ``int16`` / `array` typecode ``"h"`` on a
  little-endian host; no WAV header, no float samples, no companding;
- mono (`PCM_CHANNELS` = 1); no interleaving, ever -- a multi-mic device
  downmixes or selects a channel before reaching this boundary;
- sample rate carried by the object itself: `AudioSource.sample_rate_hz`,
  `AudioSink.sample_rate_hz`, `voice.VoiceProvider.sample_rate_hz`. Rates
  are never guessed from the buffer length; when two of them differ it is
  the caller's job to resample (WS2's ``voice/audio/resample.py``).

One millisecond of audio is therefore
``sample_rate_hz / 1000 * PCM_SAMPLE_WIDTH_BYTES`` bytes, which is how
`PlaybackTracker.remaining_ms` and `played_ms` convert between the two.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

PCM_SAMPLE_WIDTH_BYTES = 2
PCM_CHANNELS = 1
PCM_BYTE_ORDER = "little"


class AudioSource(ABC):
    """A microphone. Implementations: desktop (sounddevice), Android, body-attached.

    Attributes:
        sample_rate_hz: Capture rate in Hz of the PCM16 mono chunks handed
            to ``on_chunk``. Set by the implementation before `start`.
    """

    sample_rate_hz: int

    @abstractmethod
    async def start(self, on_chunk: Callable[[bytes], None]) -> None:
        """Start capturing PCM16 mono audio at ``sample_rate_hz``.

        Must not block for more than 100 ms. ``on_chunk`` is invoked once per
        captured chunk; implementations that capture on a separate thread
        must marshal each call back onto the event loop themselves (e.g.
        via ``call_soon_threadsafe``) so ``on_chunk`` always runs on the
        caller's loop.

        Raises:
            RuntimeError: if the underlying device cannot be opened.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop capturing. Idempotent: safe to call when already stopped."""


class AudioSink(ABC):
    """A speaker. Implementations: desktop (sounddevice), Android, body-attached.

    Attributes:
        sample_rate_hz: Playback rate in Hz expected for the PCM16 mono
            buffers passed to `play`. A caller whose source rate differs
            resamples first; `play` never resamples silently.
    """

    sample_rate_hz: int

    @abstractmethod
    async def play(self, item_id: str, pcm16: bytes) -> None:
        """Queue PCM16 mono audio at ``sample_rate_hz`` for playback under ``item_id``.

        Returns as soon as the chunk is queued, not once it has finished
        playing. ``item_id`` is the voice provider's item id and is used by
        `PlaybackTracker.played_ms` for barge-in truncation.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop playback and discard queued audio. Idempotent."""

    @abstractmethod
    def tracker(self) -> PlaybackTracker:
        """Return the `PlaybackTracker` bound to this sink's real playback head."""


class PlaybackTracker(ABC):
    """Tracks the real hardware playback head, not the write buffer.

    Neon's echo bug (plan.md section 2, item 1) came from reopening the mic
    200 ms after "logical" playback end while ~340 ms of audio were still in
    the hardware buffer. Every method here must reflect what is audibly
    coming out of the speaker right now, not what has been written to it.
    """

    @abstractmethod
    def remaining_ms(self) -> float:
        """Milliseconds of audio still queued or sounding. 0 when idle."""

    @abstractmethod
    def is_playing(self) -> bool:
        """True while audio is audibly playing (``remaining_ms() > 0``).

        Callers gate the microphone on this plus a fixed tail (250 ms).
        """

    @abstractmethod
    def energy_at_head(self) -> float:
        """Instantaneous output energy in [0, 1] at the real playback head.

        Drives `FaceState.lip` / jaw servos. Must be synchronized with the
        audio actually reaching the speaker, not the write buffer.
        """

    @abstractmethod
    def played_ms(self, item_id: str) -> float:
        """Milliseconds of ``item_id`` that have actually been heard so far.

        Used to call ``conversation.item.truncate`` at the right offset on
        barge-in, so the model's context matches what the user actually
        heard.
        """

    @abstractmethod
    def flush(self) -> None:
        """Discard all queued and in-flight audio immediately (barge-in).

        Must not raise if nothing is playing.
        """
