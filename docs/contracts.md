# Contracts

This page documents every frozen contract in `src/asimoov/contracts/`. See
`CONTRACTS_FROZEN.md` for the freeze rule. Source of truth for design intent:
`../asimoov-private/plan.md` sections 1, 2, 4.3, 4.5-4.9.

## Units and conventions

Fixed once, everywhere, so no workstream has to guess:

| Thing | Unit |
|---|---|
| `ts`, `created_at`, `last_seen_at`, `started_at`, `ended_at` | Unix seconds, `float` |
| `*_s` (`timeout_s`, `duration_s`, `eta_s`, `cooldown_s`) | seconds, `float` |
| `*_ms` (`watchdog_ms`, `est_ms`, `last_rtt_ms`, `remaining_ms`, `played_ms`) | milliseconds |
| `bearing.az` / `bearing.el`, `GazeTarget.az` / `.el` | degrees, body-relative (0 = straight ahead / horizon) |
| `move(vx, vy, wz, ...)` | normalized `[-1, 1]`, clamped by `limits` |
| `FaceState.gaze.x` / `.y` | normalized `[-1, 1]` |
| `intensity`, `lip`, `eyelids`, `confidence`, `face_quality`, `urgency` | `[0, 1]` |
| `battery` (percept and `BodyHealth`) | fraction in `[0, 1]`, not a percentage |
| `pcm16` buffers | signed 16-bit little-endian, mono, rate from the object's `sample_rate_hz` |
| field names | `snake_case`, except the manifest headers `apiVersion` / `kind` |
| Python | `>=3.10`; `X | None` typing with `from __future__ import annotations` |

### Sign conventions (frozen)

- `Bearing.az` / `GazeTarget.az`: degrees, **positive = to the robot's own
  left** (counter-clockwise seen from above, ROS REP-103); `el` positive =
  up. A person standing to the robot's left has `az > 0`.
- `FaceGaze.x` (`FaceState.gaze.x`): normalized `[-1, 1]`, **positive = the
  face looks toward the robot's own left, which is the *viewer's right* on
  a screen facing the viewer** (mirror convention). A person at `az > 0`
  therefore yields `gaze.x > 0`. `gaze.y` positive = up.
- A body whose `look_at` drives a yaw joint turns that joint **left** for
  `target.az > 0`.

## Vocabularies (`vocab.py`)

Closed, versioned vocabularies (`VOCAB_VERSION = 1`):

- `ENVELOPE_KINDS`: `percept | state | cmd | reply | frame | log | metric`
- `TOPIC_PREFIXES`: `percept. scene. face. body. voice. mind. metric. log
  frame. x. perception. component. safety.`
- `PERCEPT_TYPES`: `person_seen person_lost speech_started speech_ended
  utterance touched battery body_state sound_event app_event`
- `EMOTIONS`: `neutral happy excited curious annoyed sad angry love sleeping`
- `DURATION_CLASSES`: `instant` (<300ms) `short` (<1.5s) `long` (async)
- `DISTANCE_CLASSES`: `near medium far`
- `IDENTITY_STATUSES` (v1.2): `unknown uncertain identified`
- `BODY_KINDS`: `quadruped humanoid_bust virtual wheeled other`
- `CAPABILITIES`: base capabilities plus `gesture.<name>` for each of
  `GESTURES` (wave, sit, lie_down, stand, stretch, dance, heart, nod,
  shake_head, hand_right, hand_left, point, finger_demo).

Apps extend percepts and topics under the reserved `x.<app>.*` prefix; this
namespace is never part of the closed vocabulary above.

`vocab.TOPICS` names the built-in topics as constants (`SCENE_STATE`,
`FACE_STATE`, `FACE_OVERLAY`, `BODY_CMD`, `BODY_REPLY`, `BODY_HEALTH`,
`VOICE_EVENT`, `MIND_INJECTION`, `SAFETY_ESTOP`, `LOG`), plus the
`PERCEPT_PREFIX` / `METRIC_PREFIX` / `FRAME_PREFIX` prefixes and the
`COMPONENT_HEALTH_PATTERN` naming convention (`<component>.health`, e.g.
`body.health` today).

Two of those are v1.2. `safety.estop` is the request any component makes to
cut everything (the face page's E-STOP button publishes it; the core
subscribes and calls `SafetyGuard.stop_all`). `face.overlay` is a body's
transient contribution to the face: the Mind alone publishes `face.state`,
and mixes the newest overlay on top of it for a fraction of a second.

`body.manifest` is v1.3: a body republishes its `BodyManifest` there, as a
`state` envelope, every time it learns what it is. A bust reads its channel
table from the firmware, so before its link is up it declares no capability
at all; the core rebinds the resolver, the executor, the safety guard and
the tool list on every such message.

`vocab.APP_PERMISSIONS` (`APP_PERMISSIONS_VERSION = 1`) is the closed list
an app's `permissions` (`app.v1.json`) draws from: `camera.frames`,
`llm.vision`, `memory.read`, `memory.write`, `persons.read`,
`persons.write`, `mail.send`, `network.outbound`, `body.motion`,
`body.gesture`, `audio.in`, `audio.out`.

## Bus (`bus.py`, no schema -- runtime-only)

`Bus` (a `typing.Protocol`): `publish(topic, data, *, kind="percept",
corr=None)`, `subscribe(pattern, handler) -> Subscription`
(`Subscription.unsubscribe()`), `request(topic, data, *, timeout_s) ->
Envelope` (raises `asyncio.TimeoutError` on no reply), `latest(topic) ->
Envelope | None`. Pattern syntax for `subscribe`: an exact topic or a
prefix ending in `*` (`"percept.*"`). Implementations (local in-process,
remote hub) live in core (WS1); the `Transport` ABC that backs them is not
part of `contracts`. `BodyContext.bus` and `PerceptionContext.bus` are
typed as `Bus | None`; the older `publish`/`subscribe`/`request` fields on
both contexts remain as untyped convenience passthroughs.

## Envelope (`envelope.py`, `schemas/envelope.v1.json`)

The one message shape used locally (in-process bus) and remotely (hub
WebSocket at `ws://host:7331/bus`). See
`examples/envelope.person_seen.json`.

```json
{"v":1,"kind":"percept","topic":"percept.person_seen","id":"01J9...",
 "ts":1758290400.123,"src":"perception.face_id","corr":null,"data":{...}}
```

`Envelope.id` is generated by `new_id()`, a ULID-like, time-sortable id
built from stdlib only (`os.urandom` + Crockford base32), so contracts
never needs a `ulid` package.

## Percepts (`percepts.py`, `schemas/percept.v1.json`)

One dataclass per type in `vocab.PERCEPT_TYPES`, each with `to_dict()` /
`from_dict()` and a `PERCEPT_TYPE` class attribute. `decode_percept(type,
data)` dispatches through `PERCEPT_CLASSES`. See
`examples/percept.person_seen.json`.

`PersonSeen` carries the recognizer's confidence about *who* this is
(v1.2): `identity_status` is `unknown`, `uncertain` or `identified`, and
`person_id` is only set when it is `identified`. While it is `uncertain`,
`candidate_person_id` / `candidate_name` say who is suspected -- something
the mind may ask about ("are you Sam?") and must never assume -- and
`candidate_score` (v1.3) is the similarity behind that suspicion, which is
what lets the mind say "this looks like Sam (0.52)". A producer
that omits `identity_status` gets the v1.1 meaning: `identified` when
`person_id` is set, `unknown` otherwise. See
`examples/percept.person_seen.uncertain.json`.

## Behaviors (`behaviors.py`, `schemas/behavior.v1.json`)

- `BehaviorManifest`: static description loaded from a `behaviors/*.yaml`
  file. `requires` must be a subset of the body's `capabilities` for the
  `BehaviorResolver` (WS1) to expose it to the LLM; otherwise the
  `fallback` behavior is used if satisfiable, or the behavior is hidden.
- `BehaviorCall`: a resolved invocation (`name`, `params`, `call_id`).
- `BehaviorResult` / `BehaviorStatus`: `ok | started | error | timeout |
  unsupported`. `long` behaviors return `started` with `action_id` and
  `eta_s`; the real outcome follows later as a mind injection, never as a
  second `BehaviorResult`.

See `examples/behavior.shake_hand.yaml`.

## Body (`body.py`, `schemas/body.v1.json`)

`BodyManifest.implements` maps a behavior name to how this body performs
it (`{"primitive": ..., "arg": ..., "est_ms": ...}`). The `Body` ABC:

| Method | Timing | Notes |
|---|---|---|
| `start(ctx)` | must not block > 100 ms | real connection in an internal task |
| `stop()` | -- | idempotent |
| `health()` | non-blocking | last known state |
| `gesture(name, params, timeout_s=)` | waits up to `timeout_s` | never a fabricated `ok` |
| `move(vx, vy, wz, duration_s)` | `long` | clamped by `limits` |
| `look_at(target)` | fire-and-forget, <=5 Hz | body smooths |
| `set_face(state)` | <=25 Hz | no-op without `face.*` |
| `stop_all(reason)` | < 200 ms | idempotent, called by `SafetyGuard` |
| `audio_source()` / `audio_sink()` | -- | `None` if not applicable |

A body may additionally implement `async def simulate_disconnect(self) ->
None` (not part of the `Body` ABC, checked structurally). If present,
`run_conformance` calls it and asserts it triggers `stop_all` within 1 s
and that `manifest.safety["stop_on_disconnect"]` is declared. See
`examples/body.go2.yaml` and `src/asimoov/bodies/conformance.py` for
the full suite every adapter must pass (`run_conformance(body)`).

## Face (`face.py`, `schemas/face_state.v1.json`)

`FaceState` is published on `face.state` at up to 20 Hz while changing,
2 Hz otherwise. `gaze` comes from the Scene, `lip` from
`PlaybackTracker.energy_at_head()`, `blink` is core-driven so every
renderer blinks together. `FaceRenderer` ABC: `start(ctx)`, `render(state)`
(<=25 Hz, drop frames rather than fall behind), `stop()`.
`ProcessRequestHook` is the `process_request(path) -> response | None`
signature WS6 implements and WS1's hub calls, so the same port (7331)
serves both the bus WebSocket and the face page. See
`examples/face_state.curious.json`.

A renderer that serves a page exposes three **optional attributes on the
instance**, which the core reads with `getattr` and mounts on the hub
(`Runtime._mount_face`):

| attribute | type | what the hub does with it |
|---|---|---|
| `process_request` | `ProcessRequestHook` | answers `GET /face/...` as plain HTTP |
| `ws_path` | `str` | the path whose WebSocket connections are routed to this renderer |
| `attach` | `async attach(connection, *, path)` | owns that connection until it closes (its own token check, its own protocol) |

A renderer with none of them (Kivy, servos, LEDs) needs no hub at all.

## Audio (`audio.py`, no schema -- PCM16 never crosses the bus as JSON)

`AudioSource` / `AudioSink` capture and play PCM16: raw, headerless,
signed 16-bit little-endian, mono (`PCM_SAMPLE_WIDTH_BYTES`, `PCM_CHANNELS`,
`PCM_BYTE_ORDER`), at the rate each object declares in `sample_rate_hz`
(`VoiceProvider.sample_rate_hz` defaults to 24000, the OpenAI Realtime
rate). Rates are never inferred from buffer length; whoever bridges two
different rates resamples. `PlaybackTracker`
tracks the *real* hardware playback head (`remaining_ms`, `is_playing`,
`energy_at_head`, `played_ms(item_id)`, `flush()`), the fix for Neon's echo
bug (200 ms mic reopen while ~340 ms of audio were still in the hardware
buffer). `fakes.py` ships `FakeAudioSource` (scripted or silent PCM16
chunks), `FakeAudioSink` (records writes, exposes `tracker()`) and
`FakePlaybackTracker` (a simulated head advanced explicitly via
`advance_ms`, not real time, for deterministic tests).

`PlaybackTracker` also has three optional push hooks (v1.2),
`set_energy_callback(cb, loop=None)`, `set_timestamp_callback(cb,
loop=None)` and `set_loop(loop)`, all defaulting to a no-op. A tracker
backed by a real device knows when its head moved, so it pushes the energy
that drives `FaceState.lip` instead of being polled at 20 Hz; `loop` is the
event loop the device thread marshals the call onto. A caller registers the
callbacks and keeps polling if nothing ever arrives.

## Frames (`frames.py`, no schema -- binary, never JSON)

The one codec for binary `frame.*` messages, shared by the face page, the
Go2's front camera and perception's `ws_frames` source. The hub relays
these bytes between clients untouched and the core never subscribes to them
(plan.md section 4.3).

`encode_frame(topic, jpeg, *, seq=0, ts_ms=0) -> bytes` and
`decode_frame(message, *, default_topic=None) -> FrameMessage`
(`topic`, `seq`, `ts_ms`, `jpeg`). Layout, big-endian:

| bytes | field | meaning |
|---|---|---|
| 0 | `ver` | always 1 (`FRAME_VERSION`) |
| 1 | `tlen` | length in bytes of the UTF-8 topic that follows the header |
| 2-3 | `seq` | wrapping frame counter (uint16), for drop detection |
| 4-7 | `ts_ms` | capture time, Unix milliseconds modulo 2**32 |

then `tlen` bytes of topic (`frame.browser`, `frame.go2`, ...) and the JPEG
payload to the end of the message. The topic is spelled out rather than
numbered so a new camera needs no registry entry on both sides. A message
that is a bare JPEG (starting with `FF D8 FF`) is accepted when the caller
passes `default_topic`; anything else raises `ValueError` rather than being
silently dropped.

## Voice (`voice.py`, no schema -- provider-internal protocol)

`VoiceProvider` ABC: `start(events, config)`, `stop()`, `send_audio(pcm16)`,
`inject_system_text(text)`, `request_response(instructions=None)`,
`cancel_response(response_id)` (id is mandatory -- Neon's bug was canceling
without one and killing unrelated actions), `truncate_item(item_id,
played_ms)`. `VoiceEvents`: `on_speech_started/ended`, `on_utterance`,
`on_tool_call`, `on_audio_out`, `on_response_done`.

Three optional members, each with a working default so no existing
implementation breaks:

- `VoiceProvider.send_tool_result(call_id, result)` -- returns the whole
  `ToolResult` to the model. The default is a no-op, for a provider with no
  function-calling channel; the core logs once that tool results are staying
  on the bus rather than letting them vanish.
- `VoiceEvents.on_assistant_text(text)` (v1.2) -- the assistant's own
  transcript for a finished response, which the core journals so an episode
  summary covers both sides of the conversation.
- `VoiceEvents.on_response_started(response_id)` (v1.3) -- emitted for
  every response, including the ones the model starts on its own. Without
  it a core only knows about the responses it requested itself and injects
  system text into the middle of the robot's speech.

## Perception (`perception.py`, no schema -- process-internal)

`PerceptionModule` ABC: `start(ctx)`, `stop()`, `handle_command(name,
params)` (e.g. `face_id.enroll`), returning the payload for a `reply`
envelope, `{"ok": False, "reason": ...}` on real failure, never a hang.

## Memory (`memory.py`, no schema -- SQLite-internal)

`Person`, `Fact`, `Episode`, `JournalEntry` dataclasses; `MemoryStore` ABC:
`get_person`, `find_person_by_name`, `upsert_person`, `add_face_embedding`,
`match_face(vec)`, `add_fact`, `recall(query, k)` (text only, never
embeddings or numeric aggregates), `start_episode` / `end_episode`,
`journal`.

Two optional methods (v1.3), not abstract, so a v1.2 store still
instantiates: `delete_person(person_id) -> bool` forgets an identity, their
facts, the episodes they took part in and their face embeddings in one
transaction (the default raises `NotImplementedError`, so "forget me"
reports a refusal rather than pretending), and `reload_gallery() -> int`
rebuilds the in-RAM face gallery and returns its size (default `0`).

One SQLite schema serves both the memory store and perception's face store:
`core/memory/schema.sql`, WAL on.

## Persona (`persona.py`, `schemas/persona.v1.json`)

`Persona` extends the shape of Neon's `personality.yaml`: `identity`,
`traits`, `speaking_style`, `relationships` (owners/strangers), `emotions`
(subset of `vocab.EMOTIONS`), `initiative` (level/greet/cooldown/quiet
hours), `rules`, `voice`, `fragments` (filled by apps). `load_persona(path)`
raises `PersonaError` (a `ValueError`) naming the file and the offending
field on any malformed input. See `examples/persona.neon.yaml`.

## App manifest (`app_manifest.py`, `schemas/app.v1.json`)

`AppManifest`: `permissions` (denied by default, granted per-robot in
`robot.yaml: apps_permissions`; each item is one of `vocab.APP_PERMISSIONS`,
enforced by `schemas/app.v1.json`'s enum), `provides`
(tools/behaviors/percepts/persona_fragment/triggers), `requires`
(capabilities/python), `degraded` (what to disable and what to tell the
persona when a capability is missing). No store, no signature verification
in v1. See `examples/app.scene-describer.yaml`.

## Tools (`tools.py`, no schema -- runtime-only)

`ToolSpec` (JSON-schema `params`, `timeout_s`, `duration_class`),
`ToolResult` (same status vocabulary as `BehaviorResult`,
`ToolResult.from_behavior_result` converts one to the other), `ToolHandler`
(the callable a spec is bound to at runtime).

## Fakes (`fakes.py`)

`FakeBody`, `FakeVoiceProvider`, `FakeFaceRenderer`, `InMemoryMemoryStore`,
`FakeAudioSource`, `FakeAudioSink`, `FakePlaybackTracker`. `FakeBody`
returns a `FakeAudioSource`/`FakeAudioSink` from `audio_source()`/
`audio_sink()` when its manifest declares `audio.in`/`audio.out`, and
implements `simulate_disconnect()` (calls `stop_all("simulated_disconnect")`).
Reuse these; do not write a second fake of the same kind in another
workstream's tests.
