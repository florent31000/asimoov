# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added

- contracts v1.3 -- 2026-09-19: `TOPICS.BODY_MANIFEST` (`body.manifest`, a
  body republishing what it turned out to be); optional
  `VoiceEvents.on_response_started(response_id)`;
  `PersonSeen.candidate_score`; optional `MemoryStore.delete_person` and
  `MemoryStore.reload_gallery`. All additive, all defaulted.
- The core now opens the microphone and the speaker. `core/voice_loop.py`
  resolves the devices from the body and `robot.yaml: audio` (`backend:
  auto | body | desktop | android | none`) and runs the loop
  `docs/voice.md` describes: `MicGate` on the real playback head,
  `BargeInController` while the speaker is audible, `SessionManager` for
  truncation and hot renewal, the tracker driving `FaceState.lip` and the
  telemetry marks. `asimoov doctor <robot>` now exits 1 when neither the
  body nor the host can open audio, instead of reporting a healthy robot.
- `forget_person` tool: deletes the person, their facts and their episodes,
  then asks perception to reload its gallery, so "forget me" is true
  immediately and not after the next restart.
- `asimoov enroll --hub ws://host:port`, for a robot running elsewhere.
- Episodes are written for real: opened the first time anything is said,
  closed with the renewal summary and again at shutdown.
- `tests/integration/test_runtime_openai.py`: `Runtime` + the real
  `OpenAIRealtimeProvider` + the fake server, which now refuses a second
  concurrent `response.create` with the API's
  `conversation_already_has_active_response`.
- `.gitattributes` (`* text=auto`, LF for `*.sh` and `*.ino`).
- contracts v1.2 -- 2026-09-19: `contracts/frames.py`, the one binary
  `frame.*` codec (`ver | tlen | seq | ts_ms` + UTF-8 topic + JPEG), which
  the face page, the Go2 camera and perception now share;
  `TOPIC_PREFIXES` += `perception.` / `component.` / `safety.`;
  `TOPICS.SAFETY_ESTOP` and `TOPICS.FACE_OVERLAY`; `IDENTITY_STATUSES` and
  `PersonSeen.identity_status` / `candidate_person_id` / `candidate_name`
  (schema, dataclass and a second frozen example); optional
  `VoiceProvider.send_tool_result` and `VoiceEvents.on_assistant_text`;
  optional `PlaybackTracker` push hooks (`set_energy_callback`,
  `set_timestamp_callback`, `set_loop`); the face renderer's optional
  `process_request` / `ws_path` / `attach` attributes documented.
- Integration: one bus client (`core.bus.BusClient` carries binary frames
  and replaces `perception/hubclient.py`); the Mind owns `face.state` and
  mixes a body's `face.overlay`; `body.reply` unified as
  `{action_id, name, status, reason}` across the core, the Go2 and the
  InMoov; perception notes and initiative instructions follow
  `persona.language` (FR + EN); `asimoov enroll` and
  `asimoov doctor --download-models`; entry points, package data and the
  `[desktop]` extra in `pyproject.toml`; arduino-cli and gitleaks jobs in
  CI; end-to-end tests under `tests/integration/`.

### Fixed

Code review of 2026-09-19, the joint between the core and the voice
provider (nothing assembled the two, so every bug below lived there):

- The runtime handed the provider serialized tool dicts, so `build_session`
  produced a payload the API rejected and the socket reconnected forever:
  the robot was mute. It now passes `ToolSpec` objects, `build_session`
  raises `TypeError` on anything else, and `start` builds the payload in
  the caller's frame instead of inside the reconnect loop.
- The provider and the runtime each sent `response.create` after a tool
  result. The provider is now the single owner of that follow-up.
- `injector.response_active` was never true for a response the model
  started on its own, so perception updates were injected into the middle
  of the robot's speech and `Mind.tick` raised a `RuntimeError` the
  supervisor swallowed. `on_response_started` (v1.3) fixes both;
  `request_response` during an active response is now a logged skip.
- `remember_person` wrote a row and claimed success without ever asking
  perception to learn the face. It now requests
  `perception.face_id.enroll` (12 s) and returns `error / "no perception"`
  when no module answers. It no longer overwrites `relationship`/`notes`.
- The face page's camera frames were decoded and dropped: the runtime gave
  `WebFace` no `on_frame`, and `robots/avatar/robot.yaml` had `face_id`
  disabled. Both fixed; browser frames now reach perception.
- The OpenAI key was read from `$ASIMOOV_OPENAI_API_KEY` by `doctor` and
  from `$OPENAI_API_KEY` by the provider. `core.config.Secrets` is now the
  one place: `$ASIMOOV_OPENAI_API_KEY`, the secrets file, then
  `$OPENAI_API_KEY`.
- Tasks spawned by the voice bridge and by the InMoov adapter were only
  referenced by the event loop and could be collected mid-flight.
- Hub: a binary frame was relayed twice and echoed back to its sender
  (three times the traffic, a duplicate `person_seen`); the origin of the
  message being dispatched lived in a shared attribute, so two concurrent
  publishers raced; a peer whose socket stopped draining blocked the whole
  hub. Each peer now has a bounded outbox drained by its own task, which
  drops superseded `state` snapshots first.
- InMoov: the neck, jaw and eyelid channels were never attached, so the
  head never moved; replies were not checked; after an e-stop only a
  gesture rearmed the servos; interrupting a gesture sent `!` and the arm
  fell 300 ms later; a cancelled gesture published no `body.reply`; the
  pyserial write blocked the event loop. Four simulator/firmware parity
  gaps closed (lowercase `t`, trailing junk, `LINE_MAX`, watchdog notices
  to the TCP client), all mirrored in `protocol.md`.
- Go2: `_report_move_done` published `ok` whatever happened; it now
  reports the `MotionController`'s real outcome (`ok` / `interrupted` /
  `error`). `motion._swallow` is gone and `ensure_normal_mode` verifies
  the mode actually changed.
- A body only learned its capabilities once its link was up, and the core
  bound them once, ~90 ms after `start`. Bodies now republish on
  `body.manifest` and the core rebinds the resolver, the executor, the
  safety guard and the live session's tool list.
- `SafetyGuard.stop_all` stopped the body before cancelling the running
  behaviors, so a behavior cancelled afterwards could push one last
  command into a stopped body. The cancel is now first, within a bounded
  budget.
- The renewal summary was requested mid-speech; an out-of-band response
  was attributed by arrival order; perception, the VAD model and the
  Android settings each re-derived `~/.asimoov` instead of honouring
  `$ASIMOOV_HOME`; the perception store and the memory store opened the
  same file with two diverging schemas and no WAL; episodes were never
  written; the prompt's memories were loaded once, before anyone arrived.
- Swallowed failures replaced with `log.exception` plus a health error in
  `motion`, `perception/__main__` and the InMoov loops.
- `localtime` on a scaled clock, `WebFace.render` throttling against wall
  time during a replay, and the hub token file created world-readable.

Earlier:

- The core passed `BodyManifest.implements[...]["arg"]` to `Body.gesture`
  while every body (and the frozen conformance suite) keys on the behavior
  name: every Go2 gesture failed with "no sport command for gesture 'Sit'".
- The runtime snapshotted the body manifest before `Body.start`, so an
  InMoov bust -- which reads its channel table from the firmware -- exposed
  none of its gestures to the model.
- The runtime handed `send_tool_result` a dict where the contract (and the
  OpenAI provider) expect a `ToolResult`.
- `asyncio.wait_for` in supervised loops of the Go2, the InMoov and the
  voice provider replaced with `core.timeouts.run_with_timeout`, which does
  not turn an outer cancellation into a `TimeoutError`.

- contracts v1.1 -- 2026-09-19: `contracts/bus.py` (`Bus` protocol,
  `Subscription`), `BodyContext.bus` / `PerceptionContext.bus` typed as
  `Bus | None`; `vocab.TOPICS` and `vocab.APP_PERMISSIONS`; `app.v1.json`
  `permissions` enum; frozen sign conventions for `bearing.az` /
  `GazeTarget.az` / `FaceGaze.x` (docstrings corrected, documented in
  `docs/contracts.md`); audio fakes (`FakeAudioSource`, `FakeAudioSink`,
  `FakePlaybackTracker`), wired into `FakeBody`; optional
  `simulate_disconnect()` conformance check; `robots/avatar/behaviors/`
  and `robots/README.md`.
- Repository skeleton and frozen contracts v1 (`src/asimoov/contracts/`):
  vocabularies, ABCs (`Body`, `FaceRenderer`, `VoiceProvider`,
  `PerceptionModule`, `MemoryStore`), 7 JSON Schemas with examples, shared
  fakes, `Body` conformance suite, `pyproject.toml`, CI.
