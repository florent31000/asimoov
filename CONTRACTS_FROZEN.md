# Contracts freeze

**Version:** v1.4
**Frozen:** 2026-09-20
**Owner:** WS0

`src/asimoov/contracts/**` (vocabularies, ABCs, JSON Schemas, examples, and
the shared fakes) is frozen at v1.4. Every other workstream builds against
this surface without modifying it.

## v1.4 additions (additive, backward-compatible)

One field, with a default, so no v1.3 implementation breaks.

- `contracts/persona.py` + `schemas/persona.v1.json`: `VoiceConfig.options`
  (a mapping, default empty). A second voice provider arrived --
  `claude_pipeline`, a STT -> LLM -> TTS pipeline -- and its persona needs
  settings the Realtime provider has no use for: which STT and TTS engine,
  the effort level, the end-of-turn silence. Adding a field per provider to
  a frozen contract does not scale, so `options` carries them verbatim:
  `Runtime._voice_config` spreads it under the keys the core owns
  (`provider`, `model`, `voice`, `transcription_language`, `instructions`,
  `tools`), and `contracts/` never interprets it. `to_dict` omits it when
  empty, so a v1.3 persona round-trips byte for byte. A mapping field would
  make the generated `__hash__` raise on a class that was hashable in v1.3,
  so `VoiceConfig.__hash__` is written explicitly over the four strings;
  `__eq__` still compares `options`.
- `contracts/fakes.py`: `InMemoryMemoryStore.touch_person(person_id, seen_at)`
  and `InMemoryMemoryStore.facts_for(person_id, limit)`, matching the two
  concrete methods `core.mind.Mind` calls unconditionally on `self.memory`
  (`_note_identity` and `refresh_memories`) that `SqliteMemoryStore` already
  had and the fake did not: a mind tick against `InMemoryMemoryStore` raised
  `AttributeError` as soon as a scene held a linked person. Both are new
  methods on an already-concrete class, not ABC changes.

## v1.3 additions (additive, backward-compatible)

Everything below has a default, so no v1.2 implementation breaks.

- `contracts/vocab.py`: `TOPICS.BODY_MANIFEST` (`body.manifest`). A body
  republishes its `BodyManifest` there, as a `state` envelope, every time
  it learns what it is; the core rebinds the resolver, the executor, the
  safety guard and the tool list on every such message. A body that knows
  itself before `start` never publishes it and nothing changes.
- `contracts/voice.py`: optional `VoiceEvents.on_response_started(response_id)`,
  emitted on `response.created`. The model starts most responses itself, so
  a core that only tracked the ones it requested injected system text into
  the middle of the robot's speech. Callers go through
  `getattr(events, "on_response_started", None)`; providers that cannot
  tell simply never call it.
- `contracts/percepts.py` + `schemas/percept.v1.json`: `PersonSeen` gains
  `candidate_score` (0..1, default None), the similarity behind
  `candidate_person_id` while `identity_status` is `uncertain`. It is what
  lets the mind say "this looks like Sam (0.52)" instead of asserting an
  identity. `examples/percept.person_seen.uncertain.json` carries it.
- `contracts/memory.py`: optional `MemoryStore.delete_person(person_id)`
  (default raises `NotImplementedError`, so "forget me" reports a refusal
  rather than pretending) and `MemoryStore.reload_gallery()` (default `0`).
  Neither is abstract, so a v1.2 store still instantiates.
- `contracts/fakes.py`: `InMemoryMemoryStore.delete_person` cascades to
  facts and episodes.

## v1.2 additions (additive, backward-compatible)

Everything below has a default, so no v1.1 implementation breaks.

- `contracts/frames.py` (new): the single binary `frame.*` codec --
  `encode_frame`, `decode_frame`, `FrameMessage`, `FRAME_HEADER`
  (`ver | tlen | seq | ts_ms`, big-endian, then the UTF-8 topic, then the
  JPEG). Byte layout documented in `docs/contracts.md`. The face page, the
  Go2's front camera and perception's `ws_frames` source all use it;
  `perception/frames.py` is now a thin re-export.
- `contracts/vocab.py`: `TOPIC_PREFIXES` += `perception.`, `component.`,
  `safety.`; `TOPICS.SAFETY_ESTOP` (`safety.estop`) and
  `TOPICS.FACE_OVERLAY` (`face.overlay`); `IDENTITY_STATUSES`
  (`unknown | uncertain | identified`).
- `contracts/percepts.py` + `schemas/percept.v1.json`: `PersonSeen` gains
  `identity_status`, `candidate_person_id`, `candidate_name`.
  `identity_status` defaults to the v1.1 meaning (`identified` when
  `person_id` is set, `unknown` otherwise). New frozen example
  `examples/percept.person_seen.uncertain.json`.
- `contracts/voice.py`: optional `VoiceProvider.send_tool_result(call_id,
  result)` (default no-op) and optional `VoiceEvents.on_assistant_text(text)`.
- `contracts/audio.py`: optional `PlaybackTracker.set_energy_callback`,
  `set_timestamp_callback`, `set_loop` (all default no-ops), so a tracker
  backed by a real device pushes the audible head instead of being polled.
- `contracts/face.py`: the optional attributes a renderer serving a page
  exposes on its instance -- `process_request`, `ws_path`,
  `attach(connection, *, path)` -- are documented, and the hub mounts them
  automatically.
- `contracts/fakes.py`: `FakeVoiceProvider.send_tool_result` records into
  `tool_results`.

## v1.1 additions (additive, backward-compatible)

- `contracts/bus.py` (new): `Bus` protocol (`publish`, `subscribe`,
  `request`, `latest`), `Subscription`. `BodyContext.bus` and
  `PerceptionContext.bus` are now typed as `Bus | None` (the existing
  `publish`/`subscribe`/`request` fields on both are unchanged).
- `contracts/vocab.py`: `TOPICS` (canonical topic name constants),
  `APP_PERMISSIONS` / `APP_PERMISSIONS_VERSION` / `is_app_permission`.
- `schemas/app.v1.json`: `permissions` items are now an enum of
  `vocab.APP_PERMISSIONS`.
- Sign conventions frozen and documented (docstrings +
  `docs/contracts.md`): `Bearing.az` / `GazeTarget.az` positive = the
  robot's own left; `FaceGaze.x` positive = the face looks toward the
  robot's own left (the viewer's right on screen). A prior docstring on
  `FaceState.gaze` stating the opposite has been corrected.
- `contracts/fakes.py`: `FakeAudioSource`, `FakeAudioSink`,
  `FakePlaybackTracker`. `FakeBody.audio_source()`/`audio_sink()` now
  return these when the manifest declares `audio.in`/`audio.out`, and
  `FakeBody` implements the new optional `simulate_disconnect()` protocol.
- `bodies/conformance.py`: `run_conformance` now also checks the optional
  `simulate_disconnect()` protocol when a body implements it (asserts
  `stop_all` runs within 1 s and `manifest.safety.stop_on_disconnect` is
  declared).
- `robots/avatar/behaviors/` (new, with a `README.md`) so
  `robot.yaml: behaviors_dir` resolves; `robots/README.md` (new).

## Rule

Changes to `contracts/` are **additive only**: a new field with a default,
a new enum value, a new optional method with a default no-op is fine; a
renamed field, a removed value, a signature change to an existing ABC
method is not. Any such change is a new version (v1 -> v1.1 -> v1.2 -> v1.3
-> v1.4) and goes through WS0, tagged `contracts-v1.4-frozen` and so on.

## What is frozen

- `contracts/vocab.py` -- capabilities, percept types, emotions, duration
  classes, envelope kinds, distance classes, identity statuses, body kinds,
  gestures, `TOPICS`, `APP_PERMISSIONS`.
- `contracts/frames.py` -- the binary `frame.*` codec.
- `contracts/bus.py` -- `Bus` protocol, `Subscription`.
- `contracts/envelope.py` -- `Envelope`, `new_id()`.
- `contracts/percepts.py` -- `Bearing` and one dataclass per percept type.
- `contracts/behaviors.py` -- `BehaviorManifest`, `BehaviorCall`,
  `BehaviorResult`, `BehaviorStatus`.
- `contracts/body.py` -- `BodyManifest`, `GazeTarget`, `BodyContext`,
  `BodyHealth`, the `Body` ABC.
- `contracts/face.py` -- `FaceState`, `FaceGaze`, the `FaceRenderer` ABC,
  `ProcessRequestHook` / `ProcessRequestResponse`.
- `contracts/audio.py` -- `AudioSource`, `AudioSink`, `PlaybackTracker`.
- `contracts/voice.py` -- `VoiceProvider` ABC, `VoiceEvents`.
- `contracts/perception.py` -- `PerceptionModule` ABC, `PerceptionContext`.
- `contracts/memory.py` -- `Person`, `Fact`, `Episode`, `JournalEntry`, the
  `MemoryStore` ABC.
- `contracts/persona.py` -- `Persona` and nested dataclasses (including
  `VoiceConfig.options`), `load_persona`.
- `contracts/app_manifest.py` -- `AppManifest` and nested dataclasses.
- `contracts/tools.py` -- `ToolSpec`, `ToolResult`, `ToolHandler`.
- `contracts/schemas/*.json` -- the 7 JSON Schemas (draft 2020-12).
- `contracts/examples/*` -- one frozen example per schema (two for
  `percept.v1.json`), used by `tests/contracts` and safe to reuse in other
  workstreams' tests.
- `contracts/fakes.py` -- `FakeBody`, `FakeVoiceProvider`,
  `FakeFaceRenderer`, `InMemoryMemoryStore`, `FakeAudioSource`,
  `FakeAudioSink`, `FakePlaybackTracker`.
- `src/asimoov/bodies/conformance.py` -- `run_conformance`, the suite every
  `Body` adapter must pass (including the optional `simulate_disconnect()`
  check).
- `pyproject.toml` -- distribution metadata, extras, entry points. Adding a
  dependency anywhere is an issue against WS0, not a direct edit.

## Verification

`pytest tests/contracts` and `ruff check .` are green as of this freeze
(see the WS0 completion report for the exact output). CI
(`.github/workflows/ci.yml`) runs both on every push/PR against Python
3.10 and 3.12.
