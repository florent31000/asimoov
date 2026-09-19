# Contracts freeze

**Version:** v1.1
**Frozen:** 2026-09-19
**Owner:** WS0

`src/asimoov/contracts/**` (vocabularies, ABCs, JSON Schemas, examples, and
the shared fakes) is frozen at v1.1. Every other workstream builds against
this surface without modifying it.

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
method is not. Any such change is a new version (v1 -> v1.1) and goes
through WS0, tagged `contracts-v1.1-frozen` and so on.

## What is frozen

- `contracts/vocab.py` -- capabilities, percept types, emotions, duration
  classes, envelope kinds, distance classes, body kinds, gestures, `TOPICS`,
  `APP_PERMISSIONS`.
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
- `contracts/persona.py` -- `Persona` and nested dataclasses, `load_persona`.
- `contracts/app_manifest.py` -- `AppManifest` and nested dataclasses.
- `contracts/tools.py` -- `ToolSpec`, `ToolResult`, `ToolHandler`.
- `contracts/schemas/*.json` -- the 7 JSON Schemas (draft 2020-12).
- `contracts/examples/*` -- one frozen example per schema, used by
  `tests/contracts` and safe to reuse in other workstreams' tests.
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
