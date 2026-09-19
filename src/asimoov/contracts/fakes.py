"""Shared fakes: the only test doubles every workstream is expected to reuse.

Do not write a second `FakeBody` or `FakeVoiceProvider` elsewhere; extend
these if a workstream's tests need more recorded state.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from asimoov.contracts.audio import PCM_CHANNELS, PCM_SAMPLE_WIDTH_BYTES, AudioSink, AudioSource
from asimoov.contracts.audio import PlaybackTracker as PlaybackTrackerABC
from asimoov.contracts.behaviors import BehaviorResult
from asimoov.contracts.body import Body, BodyContext, BodyHealth, BodyManifest, GazeTarget
from asimoov.contracts.envelope import new_id
from asimoov.contracts.face import FaceRenderer, FaceState
from asimoov.contracts.memory import Episode, Fact, JournalEntry, MemoryStore, Person
from asimoov.contracts.voice import VoiceEvents, VoiceProvider

_DEFAULT_FAKE_BODY_MANIFEST = BodyManifest(
    name="fake",
    kind_of_body="virtual",
    capabilities=(
        "gesture.wave",
        "gesture.sit",
        "gaze.pan_tilt",
        "face.screen",
        "audio.out",
        "audio.in",
    ),
    implements={
        "wave_hello": {"primitive": "gesture", "arg": "wave", "est_ms": 500},
        "sit": {"primitive": "gesture", "arg": "sit", "est_ms": 500},
    },
    limits={"max_speed": 1.0, "max_yaw_rate": 1.0, "max_continuous_motion_s": 20},
    safety={"watchdog_ms": 500, "stop_on_disconnect": True, "forbidden": []},
)


class FakePlaybackTracker(PlaybackTrackerABC):
    """Simulated playback head for tests, driven by an explicit fake clock
    instead of real audio hardware.

    Items are queued by `FakeAudioSink.play` (via the internal `_enqueue`);
    tests move audio through the simulated head with `advance_ms`, so
    barge-in / lip-sync timing is deterministic and does not depend on
    wall-clock sleeps.
    """

    def __init__(self) -> None:
        self._queue: list[tuple[str, float]] = []  # (item_id, duration_ms), not yet started
        self._current_item: str | None = None
        self._current_remaining_ms: float = 0.0
        self._played_ms: dict[str, float] = {}

    def _enqueue(self, item_id: str, duration_ms: float) -> None:
        self._played_ms.setdefault(item_id, 0.0)
        self._queue.append((item_id, duration_ms))
        if self._current_item is None:
            self._advance_to_next()

    def _advance_to_next(self) -> None:
        if self._queue:
            self._current_item, self._current_remaining_ms = self._queue.pop(0)
        else:
            self._current_item = None
            self._current_remaining_ms = 0.0

    def advance_ms(self, ms: float) -> None:
        """Move the fake clock forward by ``ms``, playing queued audio through it."""
        remaining = ms
        while remaining > 0 and self._current_item is not None:
            step = min(remaining, self._current_remaining_ms)
            self._played_ms[self._current_item] += step
            self._current_remaining_ms -= step
            remaining -= step
            if self._current_remaining_ms <= 0:
                self._advance_to_next()

    def remaining_ms(self) -> float:
        return self._current_remaining_ms + sum(duration for _, duration in self._queue)

    def is_playing(self) -> bool:
        return self.remaining_ms() > 0

    def energy_at_head(self) -> float:
        return 1.0 if self._current_item is not None else 0.0

    def played_ms(self, item_id: str) -> float:
        return self._played_ms.get(item_id, 0.0)

    def flush(self) -> None:
        self._queue.clear()
        self._current_item = None
        self._current_remaining_ms = 0.0


class FakeAudioSink(AudioSink):
    """Records every played chunk instead of touching a real speaker.

    Chunk byte length is converted to milliseconds using ``sample_rate_hz``
    and PCM16 mono framing, then fed to a `FakePlaybackTracker`: advance
    time in tests via ``sink.tracker().advance_ms(...)``.
    """

    def __init__(self, sample_rate_hz: int = 24000) -> None:
        self.sample_rate_hz = sample_rate_hz
        self.written: list[tuple[str, bytes]] = []
        self._tracker = FakePlaybackTracker()
        self.stop_count = 0

    async def play(self, item_id: str, pcm16: bytes) -> None:
        self.written.append((item_id, pcm16))
        bytes_per_ms = self.sample_rate_hz / 1000 * PCM_SAMPLE_WIDTH_BYTES * PCM_CHANNELS
        duration_ms = len(pcm16) / bytes_per_ms if bytes_per_ms else 0.0
        self._tracker._enqueue(item_id, duration_ms)

    async def stop(self) -> None:
        self.stop_count += 1
        self._tracker.flush()

    def tracker(self) -> PlaybackTrackerABC:
        return self._tracker


class FakeAudioSource(AudioSource):
    """Yields silent or scripted PCM16 mono chunks instead of a real microphone.

    Construct with ``chunks`` to replay specific bytes via `emit_chunks`, or
    call `emit_silence` to synthesize digital silence of a given duration.
    """

    def __init__(self, sample_rate_hz: int = 24000, chunks: list[bytes] | None = None) -> None:
        self.sample_rate_hz = sample_rate_hz
        self._chunks = list(chunks or [])
        self._on_chunk: Callable[[bytes], None] | None = None
        self.started = False

    async def start(self, on_chunk: Callable[[bytes], None]) -> None:
        self._on_chunk = on_chunk
        self.started = True

    async def stop(self) -> None:
        self.started = False

    def emit_chunks(self) -> None:
        """Replay every chunk passed to the constructor, in order."""
        if self._on_chunk is None:
            raise RuntimeError("FakeAudioSource.start() must be called before emit_chunks()")
        for chunk in self._chunks:
            self._on_chunk(chunk)

    def emit_silence(self, *, count: int = 1, chunk_ms: float = 20.0) -> None:
        """Emit ``count`` chunks of ``chunk_ms`` of digital silence."""
        if self._on_chunk is None:
            raise RuntimeError("FakeAudioSource.start() must be called before emit_silence()")
        bytes_per_ms = self.sample_rate_hz / 1000 * PCM_SAMPLE_WIDTH_BYTES * PCM_CHANNELS
        silence = b"\x00" * int(bytes_per_ms * chunk_ms)
        for _ in range(count):
            self._on_chunk(silence)


class FakeBody(Body):
    """Records every primitive call instead of touching real hardware.

    ``latency_s`` simulates connection/command latency for timeout tests.
    Passes `asimoov.bodies.conformance.run_conformance`.
    """

    def __init__(self, manifest: BodyManifest | None = None, *, latency_s: float = 0.0) -> None:
        self.manifest = manifest or _DEFAULT_FAKE_BODY_MANIFEST
        self._latency_s = latency_s
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.gaze_targets: list[GazeTarget] = []
        self.face_states: list[FaceState] = []
        self.last_stop_all_reason: str | None = None
        self._connected = False
        self._audio_source = (
            FakeAudioSource() if "audio.in" in self.manifest.capabilities else None
        )
        self._audio_sink = FakeAudioSink() if "audio.out" in self.manifest.capabilities else None

    async def start(self, ctx: BodyContext) -> None:
        self.calls.append(("start", {}))
        if self._latency_s:
            await asyncio.sleep(self._latency_s)
        self._connected = True

    async def stop(self) -> None:
        self.calls.append(("stop", {}))
        self._connected = False

    async def health(self) -> BodyHealth:
        return BodyHealth(connected=self._connected)

    async def gesture(self, name: str, params: dict[str, Any], *, timeout_s: float) -> BehaviorResult:
        self.calls.append(("gesture", {"name": name, "params": params}))
        if self._latency_s:
            try:
                await asyncio.wait_for(asyncio.sleep(self._latency_s), timeout=timeout_s)
            except asyncio.TimeoutError:
                return BehaviorResult.timeout(f"{name} exceeded {timeout_s}s")
        return BehaviorResult.ok()

    async def move(self, vx: float, vy: float, wz: float, duration_s: float) -> BehaviorResult:
        self.calls.append(("move", {"vx": vx, "vy": vy, "wz": wz, "duration_s": duration_s}))
        return BehaviorResult.ok()

    async def look_at(self, target: GazeTarget) -> None:
        self.gaze_targets.append(target)

    async def set_face(self, state: FaceState) -> None:
        self.face_states.append(state)

    async def stop_all(self, reason: str) -> None:
        self.calls.append(("stop_all", {"reason": reason}))
        self.last_stop_all_reason = reason
        self._connected = False

    async def simulate_disconnect(self) -> None:
        """Simulate a hardware disconnect, as `SafetyGuard` would react to one.

        Optional conformance hook (``bodies.conformance.run_conformance``
        calls it if present): triggers `stop_all` the same way a real link
        drop would.
        """
        self.calls.append(("simulate_disconnect", {}))
        await self.stop_all("simulated_disconnect")

    def audio_source(self) -> AudioSource | None:
        return self._audio_source

    def audio_sink(self) -> AudioSink | None:
        return self._audio_sink


class FakeVoiceProvider(VoiceProvider):
    """Replays a scripted list of `VoiceEvents` calls instead of a real API.

    ``script`` is a list of ``(event_name, args)`` pairs, where
    ``event_name`` matches a `VoiceEvents` method (e.g. ``"on_utterance"``).
    Call `play_script()` after `start()` to replay it.
    """

    def __init__(self, script: list[tuple[str, tuple[Any, ...]]] | None = None) -> None:
        self.script = list(script or [])
        self.events: VoiceEvents | None = None
        self.sent_audio: list[bytes] = []
        self.injected_texts: list[str] = []
        self.requested_responses: list[str | None] = []
        self.canceled_response_ids: list[str] = []
        self.truncated: list[tuple[str, float]] = []
        self.started = False

    async def start(self, events: VoiceEvents, config: dict[str, Any]) -> None:
        self.events = events
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def send_audio(self, pcm16: bytes) -> None:
        self.sent_audio.append(pcm16)

    async def inject_system_text(self, text: str) -> None:
        self.injected_texts.append(text)

    async def request_response(self, instructions: str | None = None) -> None:
        self.requested_responses.append(instructions)

    async def cancel_response(self, response_id: str) -> None:
        if not response_id:
            raise ValueError("cancel_response requires a response_id")
        self.canceled_response_ids.append(response_id)

    async def truncate_item(self, item_id: str, played_ms: float) -> None:
        self.truncated.append((item_id, played_ms))

    async def play_script(self) -> None:
        """Replay every scripted event onto the `VoiceEvents` given to `start`."""
        if self.events is None:
            raise RuntimeError("FakeVoiceProvider.start() must be called before play_script()")
        for name, args in self.script:
            getattr(self.events, name)(*args)


class FakeFaceRenderer(FaceRenderer):
    """Records every rendered `FaceState` instead of drawing anything."""

    def __init__(self) -> None:
        self.states: list[FaceState] = []
        self.started = False
        self.stopped = False

    async def start(self, ctx: dict[str, Any]) -> None:
        self.started = True

    async def render(self, state: FaceState) -> None:
        self.states.append(state)

    async def stop(self) -> None:
        self.stopped = True


class InMemoryMemoryStore(MemoryStore):
    """In-process `MemoryStore`: dict storage, substring `recall`, no vectors.

    ``match_face`` always returns None: this fake never implements real
    embedding matching, so tests that need it must fake `match_face`
    themselves rather than rely on a silently approximate default here.
    """

    def __init__(self) -> None:
        self._persons: dict[str, Person] = {}
        self._facts: list[Fact] = []
        self._episodes: dict[str, Episode] = {}
        self._journal: list[JournalEntry] = []

    async def get_person(self, person_id: str) -> Person | None:
        return self._persons.get(person_id)

    async def find_person_by_name(self, name: str) -> Person | None:
        needle = name.lower()
        for person in self._persons.values():
            if person.name and person.name.lower() == needle:
                return person
        return None

    async def upsert_person(self, person: Person) -> Person:
        self._persons[person.id] = person
        return person

    async def add_face_embedding(self, person_id: str, model: str, vec: Any, quality: float) -> None:
        return None

    async def match_face(self, vec: Any) -> tuple[str, float] | None:
        return None

    async def add_fact(self, fact: Fact) -> None:
        self._facts.append(fact)

    async def recall(self, query: str, k: int = 5) -> list[str]:
        needle = query.lower()
        texts = [fact.text for fact in self._facts if needle in fact.text.lower()]
        texts += [
            episode.summary
            for episode in self._episodes.values()
            if episode.summary and needle in episode.summary.lower()
        ]
        texts += [entry.text for entry in self._journal if needle in entry.text.lower()]
        return texts[:k]

    async def start_episode(self, participants: tuple[str, ...]) -> Episode:
        episode = Episode(id=new_id(), started_at=time.time(), participants=participants)
        self._episodes[episode.id] = episode
        return episode

    async def end_episode(self, episode_id: str, summary: str, mood: str | None = None) -> None:
        existing = self._episodes[episode_id]
        self._episodes[episode_id] = Episode(
            id=existing.id,
            started_at=existing.started_at,
            ended_at=time.time(),
            participants=existing.participants,
            summary=summary,
            mood=mood,
        )

    async def journal(self, text: str, kind: str = "note") -> None:
        self._journal.append(JournalEntry(ts=time.time(), text=text, kind=kind))
