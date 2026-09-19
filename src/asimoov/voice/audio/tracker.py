"""Playback tracking against the real hardware head, and the mic gate built on it.

Every public number here is expressed at the *audible* head, not at the write
buffer -- the fix for Neon's echo loop (plan.md section 2, item 1: mic reopened
200 ms after the logical end while ~340 ms were still in the hardware buffer).

`HeadTracker` holds the arithmetic shared by both platforms (item spans,
per-block energy, freezing on flush); `StreamPlaybackTracker` derives the head
from a pull-based output callback (desktop, sounddevice) and
`playback_android.AndroidPlaybackTracker` from
``AudioTrack.getPlaybackHeadPosition()``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import Callable

import numpy as np

from asimoov.contracts.audio import PCM_CHANNELS, PCM_SAMPLE_WIDTH_BYTES, PlaybackTracker

BYTES_PER_FRAME = PCM_SAMPLE_WIDTH_BYTES * PCM_CHANNELS
DEFAULT_TAIL_MS = 250.0
_MAX_TRACKED_ITEMS = 32


def _call(loop: asyncio.AbstractEventLoop | None, callback: Callable[..., None], *args) -> None:
    """Run ``callback`` on ``loop`` if there is one, else inline."""
    if loop is not None:
        loop.call_soon_threadsafe(callback, *args)
    else:
        callback(*args)


def block_energy(pcm16: bytes, gain: float = 4.0) -> float:
    """RMS of a PCM16 block, scaled and clamped to [0, 1] for `FaceState.lip`."""
    samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(samples * samples)))
    return min(1.0, rms / 32768.0 * gain)


class HeadTracker(PlaybackTracker):
    """Span bookkeeping for a playback head expressed in frames.

    Subclasses provide `_head_frames` (the frame index currently audible) and,
    if the device needs it, `_reset_device` (called by `flush`).
    """

    def __init__(self, sample_rate_hz: int = 24000, *, energy_gain: float = 4.0) -> None:
        self.sample_rate_hz = sample_rate_hz
        self._energy_gain = energy_gain
        self._lock = threading.RLock()
        self._spans: list[list] = []  # [item_id, start_frame, end_frame]
        self._frozen_played_ms: dict[str, float] = {}
        self._energies: deque[tuple[int, float]] = deque(maxlen=512)
        self._written_frames = 0
        self._announced: set[str] = set()
        self._on_energy: Callable[[float], None] | None = None
        self._on_timestamp: Callable[[str, float], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------ callbacks

    def set_energy_callback(
        self,
        callback: Callable[[float], None] | None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Subscribe to the audible output energy (drives `FaceState.lip`).

        Called every time the head moves. With ``loop``, the call is marshalled
        onto it with ``call_soon_threadsafe``, since the device runs on its own
        thread.
        """
        self._on_energy = callback
        self._loop = loop or self._loop

    def set_timestamp_callback(
        self,
        callback: Callable[[str, float], None] | None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Telemetry hook: ``("first_audio_played_ts", unix_s)`` per item."""
        self._on_timestamp = callback
        self._loop = loop or self._loop

    def set_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Marshal every callback onto ``loop`` (set once the device is open)."""
        self._loop = loop

    def notify_head_moved(self) -> None:
        """Fire the energy and first-audible callbacks. Called by the device."""
        with self._lock:
            energy = self.energy_at_head()
            head = self._head_frames()
            started = [
                item_id
                for item_id, start, _end in self._spans
                if head > start and item_id not in self._announced
            ]
            self._announced.update(started)
            energy_cb, timestamp_cb, loop = self._on_energy, self._on_timestamp, self._loop
        if energy_cb is not None:
            _call(loop, energy_cb, energy)
        if timestamp_cb is not None and started:
            _call(loop, timestamp_cb, "first_audio_played_ts", time.time())

    # ------------------------------------------------------- subclass hooks

    def _head_frames(self) -> int:
        raise NotImplementedError

    def _reset_device(self) -> None:
        return None

    # ------------------------------------------------------------ recording

    def note_written(self, item_id: str, pcm16: bytes) -> int:
        """Record ``pcm16`` as written for ``item_id``; returns its frame count."""
        frames = len(pcm16) // BYTES_PER_FRAME
        if frames <= 0:
            return 0
        with self._lock:
            start = self._written_frames
            self._written_frames += frames
            if self._spans and self._spans[-1][0] == item_id:
                self._spans[-1][2] = self._written_frames
            else:
                self._spans.append([item_id, start, self._written_frames])
                while len(self._spans) > _MAX_TRACKED_ITEMS:
                    dropped = self._spans.pop(0)
                    self._frozen_played_ms[dropped[0]] = self._frames_to_ms(
                        dropped[2] - dropped[1]
                    )
            self._energies.append(
                (self._written_frames, block_energy(pcm16, self._energy_gain))
            )
        return frames

    # -------------------------------------------------------- PlaybackTracker

    def remaining_ms(self) -> float:
        with self._lock:
            return self._frames_to_ms(max(0, self._written_frames - self._head_frames()))

    def is_playing(self) -> bool:
        return self.remaining_ms() > 0

    def energy_at_head(self) -> float:
        with self._lock:
            head = self._head_frames()
            for end_frame, energy in self._energies:
                if end_frame > head:
                    return energy
            return 0.0

    def played_ms(self, item_id: str) -> float:
        with self._lock:
            head = self._head_frames()
            for span_item, start, end in self._spans:
                if span_item == item_id:
                    return self._frames_to_ms(min(max(head - start, 0), end - start))
            return self._frozen_played_ms.get(item_id, 0.0)

    def flush(self) -> None:
        with self._lock:
            head = self._head_frames()
            for span_item, start, end in self._spans:
                self._frozen_played_ms[span_item] = self._frames_to_ms(
                    min(max(head - start, 0), end - start)
                )
            self._spans.clear()
            self._energies.clear()
            self._announced.clear()
            self._written_frames = 0
            for key in list(self._frozen_played_ms)[:-_MAX_TRACKED_ITEMS]:
                del self._frozen_played_ms[key]
            self._reset_device()

    def _frames_to_ms(self, frames: int) -> float:
        return frames * 1000.0 / self.sample_rate_hz


class StreamPlaybackTracker(HeadTracker):
    """Head of a pull-based output stream (desktop, `sounddevice`).

    The stream callback pulls frames with `read`, so frames it has consumed
    become audible ``output_latency_ms`` later; the audible head is
    ``consumed - latency``. That latency must come from the open device
    (PortAudio's output latency covers callback-to-DAC, buffer included);
    leaving it at 0 models an ideal device with no buffer at all.

    Args:
        sample_rate_hz: Rate of the PCM16 mono audio fed to `feed`.
        output_latency_ms: Hardware latency of the open stream, set by
            `set_output_latency_ms` once the device reports it.
        energy_gain: Scaling applied to the block RMS before clamping to [0, 1].
    """

    def __init__(
        self,
        sample_rate_hz: int = 24000,
        *,
        output_latency_ms: float = 0.0,
        energy_gain: float = 4.0,
    ) -> None:
        super().__init__(sample_rate_hz, energy_gain=energy_gain)
        self._latency_frames = int(output_latency_ms * sample_rate_hz / 1000)
        self._queue: deque[tuple[str, bytes, int]] = deque()
        self._consumed_frames = 0

    def set_output_latency_ms(self, latency_ms: float) -> None:
        with self._lock:
            self._latency_frames = int(latency_ms * self.sample_rate_hz / 1000)

    def feed(self, item_id: str, pcm16: bytes) -> None:
        """Queue one chunk of PCM16 mono audio under ``item_id``."""
        if not pcm16:
            return
        with self._lock:
            self._queue.append((item_id, pcm16, 0))
            self.note_written(item_id, pcm16)

    def read(self, frames: int) -> bytes:
        """Pull ``frames`` frames for the output device, zero-padded.

        Called from the audio callback thread.
        """
        wanted = frames * BYTES_PER_FRAME
        out = bytearray()
        with self._lock:
            while len(out) < wanted and self._queue:
                item_id, chunk, offset = self._queue[0]
                take = min(wanted - len(out), len(chunk) - offset)
                out += chunk[offset : offset + take]
                if offset + take >= len(chunk):
                    self._queue.popleft()
                else:
                    self._queue[0] = (item_id, chunk, offset + take)
            if len(out) < wanted:
                out += b"\x00" * (wanted - len(out))
            # Silence padding counts too: the device consumes it, so the head
            # keeps moving and `remaining_ms` reaches 0 once the queue dries up.
            self._consumed_frames += frames
        self.notify_head_moved()
        return bytes(out)

    def _head_frames(self) -> int:
        return max(0, self._consumed_frames - self._latency_frames)

    def _reset_device(self) -> None:
        self._queue.clear()
        self._consumed_frames = 0


class MicGate:
    """Whether the microphone may be forwarded to the voice provider.

    Closed while the tracker reports audio at the head, and for ``tail_ms``
    after the last audible frame. Works with any `PlaybackTracker`, including
    the frozen `FakePlaybackTracker`.
    """

    def __init__(
        self,
        tracker: PlaybackTracker,
        *,
        tail_ms: float = DEFAULT_TAIL_MS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tracker = tracker
        self._tail_s = tail_ms / 1000.0
        self._clock = clock
        self._ended_at: float | None = None
        self._was_playing = False

    def is_open(self) -> bool:
        if self._tracker.is_playing():
            self._was_playing = True
            self._ended_at = None
            return False
        if self._was_playing:
            self._was_playing = False
            self._ended_at = self._clock()
        if self._ended_at is None:
            return True
        return self._clock() - self._ended_at >= self._tail_s

    def ungate(self) -> None:
        """Open the gate immediately (barge-in: the user is already talking)."""
        self._was_playing = False
        self._ended_at = None
