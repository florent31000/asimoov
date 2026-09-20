# Architecture

ASIMOOV runs entirely on the machine next to the robot: the robot's own
computer, a PC or Raspberry Pi on the home Wi-Fi, or an Android phone. The
only outbound calls are to the LLM/voice provider. There is no ASIMOOV
server.

This page describes the core (`src/asimoov/core/`): what runs, in which
task, and how the other workstreams plug into it. The frozen data contracts
live in [contracts.md](contracts.md).

## One process, one event loop

`Runtime` (`core/runtime.py`) assembles a robot from a `RobotConfig` and
owns a single asyncio loop:

```
LocalBus ── Hub (WebSocket 7331, optional)
   │
   ├── Body          (in-process, or RemoteBody over the hub)
   ├── SafetyGuard   supervised task: polls body.health(), e-stop
   ├── Mind          supervised tasks: 1 Hz tick + 20 Hz face
   ├── FaceRenderers subscribed to face.state
   ├── VoiceProvider events published on the bus by a thin bridge
   ├── BehaviorResolver / BehaviorExecutor
   ├── ToolRegistry  (behavior tools + builtins, exported over MCP)
   ├── VoiceLoop     mic -> gate/barge-in -> provider, provider -> speaker
   └── MemoryStore   SQLite + FTS5, all SQL in a worker thread
```

Every long-lived component runs in a named task owned by `Supervisor`,
which restarts it with exponential backoff (0.5 s doubling to 30 s, reset
after a minute of healthy run) and publishes a snapshot on
`component.health`. Audio capture and playback live in their own threads
(WS2) and reach the loop through `call_soon_threadsafe`; perception is a
separate process attached to the hub.

Nothing in the core calls an LLM. The mind decides what the model is told
and when it is asked to speak; the model only ever acts through tools.

## Bus

`LocalBus` implements `contracts.bus.Bus`: `publish`, `subscribe`,
`request`, `latest`. Handlers are awaited in subscription order inside
`publish`, so a `reply` published by a responder is delivered before
`request` awaits its future; a component that needs to take its time
spawns its own task. Envelopes of kind `state` are retained per topic
(latest-value cache) and replayed to a client when it connects.

`Hub` (`core/bus/hub.py`) serves everything on one port (7331):
the bus WebSocket, the face page over plain HTTP, routed WebSocket paths,
and binary frames relayed between clients.

Two guarantees anyone writing a client needs:

- a message is relayed **exactly once** to each interested peer, and
  **never** back to the peer that sent it — envelope or binary frame alike;
- each peer has a **bounded outbox** (`SEND_QUEUE_MAX`, 64) drained by its
  own task, so a slow socket never slows the hub down. When that outbox
  fills, superseded `state` snapshots of the same topic are dropped first
  (a newer snapshot says everything the older one did); if there is nothing
  to reclaim, the incoming message is dropped and logged at WARNING.
  Delivery is therefore ordered, but not lossless for a peer that cannot
  keep up.

### The hub protocol, exactly

```
client                                   hub (ws://host:7331/bus?token=...)
  |  GET /bus?token=<~/.asimoov/token>     |
  |--------------------------------------->|  401 "invalid token" if it does not
  |                                        |  match (constant-time compare)
  |  {"type":"hello","module_id":"perception",
  |   "subscribe":["percept.*","frame.*"]} |
  |--------------------------------------->|
  |  {"type":"welcome","module_id":...,"v":1}
  |<---------------------------------------|
  |  every retained `state` envelope matching the patterns
  |<---------------------------------------|
  |  envelope.v1 JSON, both ways           |
  |<-------------------------------------->|
  |  {"type":"subscribe","patterns":[...]} |  replaces the pattern list
  |--------------------------------------->|
  |  binary frame.* messages, both ways    |  relayed untouched
  |<-------------------------------------->|
```

Three details differ from a naive reading and matter to anyone writing a
client:

- the `hello` is a **bare control message**, not an envelope: it has a
  `type` and no `kind`. Control messages and envelopes share the socket and
  are told apart by that. A first message that is not a `hello` closes the
  connection (code 1002), as does staying silent for 5 s.
- the token is **mandatory** and travels as a query parameter, because a
  browser cannot set a header on a WebSocket handshake.
- `{"type":"subscribe"}` **replaces** the pattern list, it does not add
  to it. `BusClient` accumulates them locally and sends the whole set.

Binary frames (`contracts/frames.py`) are relayed between clients and to
local frame handlers, and **never** enter the envelope dispatch: the core
does not subscribe to `frame.*`. A client only receives them if its
patterns cover `frame.`.

Every other HTTP path is handed to the `ProcessRequestHook` a face renderer
installs, so the face page is served from the same port; and
`Hub.route(path, handler)` hands a whole WebSocket connection to a renderer
(`/face/ws`), which does its own token check.

`BusClient` (`core/bus/client.py`) is the same `Bus` surface for another
process or machine: a `LocalBus` whose `Transport` is a reconnecting
WebSocket to the hub, carrying envelopes and binary frames
(`subscribe_frames`, `send_frame`). It is the only bus client in the tree:
perception, a remote body and the CLI's `asimoov enroll` all use it.

## Scene and attention

`SceneTracker` applies percepts and hands out immutable `SocialScene`
snapshots (`core/scene/`), published on `scene.state`. A track not seen for
8 s is forgotten on the next tick.

- Speaker attribution v1: a direction of arrival wins if a visible person
  sits within 35° of it; otherwise a single visible person is the speaker;
  otherwise whoever we are already attending to keeps the floor; otherwise
  nobody is attributed. The core never guesses a name from silence.
- Attention has hysteresis: a freshly chosen target is held for 2 s, after
  which a challenger must score 0.15 higher to take over. Someone who
  starts speaking overrides both.

## Mind

`Mind.tick` runs once per second (`core/mind/mind.py`):

1. age the scene, resolve the identities of the people present against
   memory, publish `scene.state` when it changed;
2. evaluate initiative;
3. flush the injector.

A second loop publishes `face.state` at up to 20 Hz when it changes and at
least every 0.5 s otherwise: emotion, gaze from the attention target
(`gaze.x = az / 60`, clamped), `lip` from the playback tracker, and a blink
every 3 to 6 s driven by the core so every renderer blinks together.

**The mind owns `face.state`.** A body that animates the face (the avatar
nodding, a bust moving its eyes) publishes its contribution on
`face.overlay` instead, and the mind mixes the newest overlay on top of its
own face for `OVERLAY_TTL_S` (0.25 s). When the animation stops the overlay
lapses on its own — there is no "overlay off" message, and no two
components fighting over the same topic.

**Injector** (`core/mind/injector.py`): perception notes are coalesced for
one second, never injected while a response is streaming (queued and
flushed at `response.done`), capped at six per minute, deduplicated over a
minute, truncated to 300 characters. Each delivery is published on
`mind.injection`.

**Initiative** (`core/mind/initiative.py`): v1 greets someone who just
arrived, once per track and once per person per cooldown, never during
quiet hours or while someone is speaking. The persona's `initiative.level`
scales the cooldown (low ×2, high ×0.5). A known person is greeted by name
with how long ago they were last seen.

**Prompt** (`core/mind/prompt.py`): `persona.identity` with `{name}` and
`{body_description}` filled in, then traits, speaking style,
relationships, rules, app fragments, the scene, and the memories of the
people present. `{body_description}` is derived from the body manifest, so
the same persona file describes a quadruped, a bust or an on-screen avatar.

The system prompt is English (it describes the robot to the model), but
perception notes and initiative instructions land mid-conversation, so they
follow `persona.language` (`core/mind/phrases.py`, FR and EN templates,
English for any other language). Injecting an English note into a French
conversation made the robot answer in English for a turn.

## Behaviors and tools

A behavior is a semantic action; each body says how it performs it
(`BodyManifest.implements`). `BehaviorResolver` (`core/behaviors/`) keeps
the behaviors whose `requires` is covered by the body's capabilities,
follows the `fallback` chain for the others (`shake_hand` → `wave_hello`),
and hides what remains impossible. The LLM never sees a behavior the robot
cannot honour.

`BehaviorExecutor` dispatches to the body primitives:

| duration_class | behaviour |
|---|---|
| `instant`, `short` | awaited under a 2 s budget, the body's real result is returned; a silent body yields `timeout`, never a fabricated success |
| `long` | `{"status":"started","action_id","eta_s"}` immediately, the outcome arriving later as a `[Action a1b2c3 shake_hand: done\|failed\|canceled]` injection |

Whoever finishes a long action — the core's executor, the Go2 driving its
own move, the InMoov bust running a gesture — announces it the same way, on
`body.reply` with `{action_id, name, status, reason}`. The core subscribes
and turns each one into that injection, so there is a single path from "the
movement is over" to "the model knows". `Body.gesture(name, ...)` receives
the **behavior** name; the body looks it up in its own
`BodyManifest.implements` to find the primitive argument.

`move` durations are clamped by `limits.max_continuous_motion_s`.

`ToolRegistry` (`core/tools/`) generates the tools with dynamic enums: one
`gesture(name)` whose enum is the visible gestures, plus `move`, `turn`,
`look_at`, `set_expression` when the body can do them, plus the builtins
`who_is_here`, `remember_person`, `remember_fact`, `recall` and `stop`.
`stop` bypasses everything and calls `SafetyGuard.stop_all` directly.
`asimoov mcp` exposes the same registry as a stdio MCP server
(`core/tools/mcp_export.py`, JSON-RPC by hand, no new dependency).

## Memory

`SqliteMemoryStore` (`core/memory/`) is one SQLite file with FTS5 over
facts, episode summaries and journal entries. Face embeddings are stored as
BLOBs and compared in RAM (cosine, numpy); no method returns a vector, so
nothing but text can reach a prompt. `person_id` is derived from the name
(`Sam` → `person:sam`) so a face recognized in a later session and a replay
fixture can both refer to the same person. The schema lives in one file,
`core/memory/schema.sql`, executed by both the memory store and
perception's face store (same database, WAL on). Episode summaries come
from an injected text callable; without one the summary is explicitly
marked `(extract)`.

An episode is opened the first time anything is said in a session and
closed twice over: with the out-of-band summary at every session renewal,
and with the transcript at shutdown. A session where nobody spoke leaves
no row.

"Forget me" is the `forget_person` tool: `delete_person` removes the
identity, their facts, the episodes they took part in and their face
embeddings in one transaction, then
`perception.face_id.reload_gallery` rebuilds the recognizer's in-RAM
gallery so the face stops being recognized immediately, not after the next
restart. Symmetrically, `remember_person` only reports success once
`perception.face_id.enroll` has actually learned the face; with no
perception module connected it returns `error / "no perception"`.

## Safety

`SafetyGuard` (`core/safety.py`) is independent of the body: it polls
`body.health()` every second (publishing it on `body.health`), stops
everything when the link goes quiet for more than 3 × `watchdog_ms`, when a
body declaring `stop_on_disconnect` disconnects, when the face page presses
E-STOP, or when an emergency phrase appears in an `utterance` — a plain
string match, no model involved. `stop_all` cancels the running behaviors
first (bounded by `CANCEL_TIMEOUT_S`, 1 s) and only then cuts the body: a
behavior cancelled afterwards gets to push one last command into a body
that has just been stopped.

**The spoken stop is not the reliable path.** An emergency phrase is only
matched on a *final* transcript, which arrives after the provider's VAD has
decided the user stopped talking and the transcription model has answered:
typically 1 to 3 seconds, and never at all if the network is down. The
face page's E-STOP button, the `safety.estop` topic and — on a bust — the
physical switch on the servo rail are the paths that always work and the
ones the documentation points a user at. The voice phrase is a convenience,
not a safety device.

Bounded waits use `core/timeouts.py:run_with_timeout` rather than
`asyncio.wait_for`, which on CPython 3.11 can surface an outer cancellation
as `TimeoutError` and let a supervised loop swallow its own shutdown.

## Apps

`core/apps/loader.py` loads the apps named in `robot.yaml` from the
`asimoov.apps` entry point group or from `apps/<name>/asimoov-app.yaml`.
Permissions are denied by default and granted per app in
`robot.yaml: apps_permissions`; a missing capability activates the app's own
`degraded` rules (disable these tools, tell the persona why), and an app
whose missing capability no rule covers is not loaded at all. No store, no
signature.

## Replay and telemetry

`asimoov replay tests/fixtures/replays/family_evening.jsonl --speed 4`
replays recorded envelopes through a real runtime. A replay file is JSONL:
envelope.v1 objects, optionally interleaved with assertions:

```json
{"assert":"expect","topic":"mind.injection","contains":"Sam just arrived","within_s":8}
```

An assertion waits `within_s / speed` for a matching envelope published
after the previous assertion. `--speed N` runs the mind N times faster by
swapping in a `ScaledClock` (`core/clock.py`) and dividing the tick period,
so cooldowns and hysteresis still see the real elapsed time; an idle gap
longer than `--max-gap` real seconds is skipped by moving that clock.
`tools/record_percepts.py` records a live session's percepts into the same
format.

`core/telemetry.py` appends spans and counters as JSONL under
`~/.asimoov/metrics/` (`turn` and its `first_audio_delta` /
`first_audio_played` phases, `tool_latency{name}`, counters);
`asimoov stats` prints p50/p95.

## CLI

```
asimoov run <robot_dir> [--voice fake|openai_realtime|claude_pipeline|none] [--body fake|...]
                        [--face web|none] [--replay FILE --speed N --max-gap S]
                        [--no-hub]
asimoov replay <file> [--robot DIR] [--speed N] [--max-gap S] [--body ...] [--voice ...]
asimoov enroll --name X [--robot DIR] [--track ID] [--timeout S]
asimoov stats [--metrics-dir DIR]
asimoov doctor [robot_dir] [--download-models]
asimoov mcp <robot_dir> [--body ...]
```

`asimoov enroll` talks to a robot that is **already running**: it connects
to its hub as a `BusClient`, finds the one visible person in the retained
`scene.state` (or takes `--track`), and sends a `cmd` on
`perception.face_id.enroll` with a 12 s budget. It never opens a camera
itself.

`asimoov doctor` reports what actually works here: the plugins the entry
points resolve, SQLite FTS5, the face and Silero models on disk, the audio
backends that import, and the resulting barge-in mode (full-duplex with
Silero, or full-duplex on a relative-energy detector when Silero is
missing). `--download-models` fetches and checksums them first.

Bodies, faces, voice providers and perception modules are resolved by name
from the entry point groups `asimoov.bodies`, `asimoov.faces`,
`asimoov.voice_providers`, `asimoov.perception`; `fake` is also provided by
a fallback registry (`core/plugins.py`) so `--body fake --voice fake` works
on a bare install. An unknown name lists the names that exist.

Secrets are read from `$ASIMOOV_<NAME>` (e.g. `ASIMOOV_OPENAI_API_KEY`) or
`~/.asimoov/secrets.yaml`, and never logged, printed, or included in an
error message.

## How the pieces talk: one turn, end to end

Someone the robot has never met walks in and it ends up waving at them.
Every arrow below is a bus topic, which is exactly why the whole thing
replays from a JSONL file (`tests/fixtures/replays/family_evening.jsonl`).

```
perception (own process)          core (one loop)                 body / face
        |                              |                               |
  1. percept.person_seen ------------->|  SceneTracker.apply
        |                              |--> scene.state
        |                              |--> face.state (gaze toward them)-->|
        |                              |                               render
  2.    |                  InitiativePolicy.consider -> proposal
        |                              |--> mind.injection
        |                    Injector -> VoiceProvider.inject_system_text
        |                              |     + request_response(instructions)
        |                              |--> voice.event {response_requested}
  3.    |            VoiceProvider streams audio back
        |                              |<-- on_audio_out -> AudioSink.play
        |                     PlaybackTracker pushes energy -> FaceState.lip
        |                              |--> face.state (talking, lip) ---->|
  4.    |            the model calls a tool
        |                              |<-- on_tool_call
        |                              |--> voice.event {tool_call}
        |                    ToolRegistry.call -> BehaviorResolver
        |                              |--> BehaviorExecutor -> Body.gesture("wave_hello")
        |                              |--> voice.event {tool_result}
        |                    VoiceProvider.send_tool_result(call_id, ToolResult)
  5.    |            the wave finishes (a `long` action)
        |                              |<-- body.reply {action_id,name,status,reason}
        |                              |--> mind.injection "[Action a1b2c3 wave_hello: done]"
        |                    flushed at the next response.done
```

Three rules hold at every step:

1. **Nothing in the core calls an LLM.** The mind decides what the model is
   told and when it is asked to speak; the model only ever acts through
   tools whose enums the resolver built from the body's real capabilities.
2. **Injections wait.** An injection is never sent while a response is
   streaming: the injector queues, coalesces and deduplicates, and flushes
   at `response.done` (which is what `on_response_done` publishes).
3. **No fabricated success.** A body that does not answer within its budget
   yields `timeout`, and that is what the model is told.

## Plugging into the core

**Bodies (WS4/WS5)** implement `contracts.body.Body` and pass
`asimoov.bodies.conformance.run_conformance`. `BodyContext` carries the
`config` block from `robot.yaml` plus the bus. The core never sends raw
model output to a body: only `gesture`, `move`, `look_at`, `set_face` and
`stop_all`.

A body that only learns what it is once its link is up — an InMoov bust
reads its channel table from the firmware — publishes its `BodyManifest`
on `body.manifest` (kind `state`, v1.3) at every connect and reconnect.
The core rebinds the resolver, the executor, the safety guard and the tool
list, and pushes the new tools into the live voice session. A body that
knows itself before `start` never publishes it.

**Voice providers (WS2)** implement `contracts.voice.VoiceProvider`. The
runtime calls `start(events, config)` with `config` carrying the persona's
voice block (`provider`, `model`, `voice`, `transcription_language`) plus
`instructions` (the system prompt) and `tools` (the `ToolSpec` **objects**
to advertise — the provider owns the wire format and rejects anything
else), and the provider calls back into the `VoiceEvents` it was
given. The API key comes from `core.config.Secrets`, under the key the
provider class names in its `secret_key` attribute
(`$ASIMOOV_OPENAI_API_KEY` / `$ASIMOOV_ANTHROPIC_API_KEY`, then
`~/.asimoov/secrets.yaml`, then the vendor's own variable), the same place
`asimoov doctor` reads. The runtime publishes
each event on the bus, which is what makes a session replayable:
`on_speech_started` / `on_speech_ended` / `on_utterance` become
`percept.*`; `on_tool_call` becomes a `voice.event` `tool_call`, answered
with a `voice.event` `tool_result` and, if the provider exposes
`send_tool_result(call_id, payload)`, with the real function-call output;
`on_response_started` (v1.3) and `on_response_done` become `voice.event`
`response_started` / `response_done`, which are what block and unblock the
injector — the model starts most responses itself, so the core cannot only
count the ones it asked for. `core/session.py` holds the renewal /
truncation / idle thresholds, and `core/voice_loop.py` opens the devices
and runs the microphone loop (see `docs/voice.md`).

Two providers ship. `openai_realtime` is speech to speech over one
websocket. `claude_pipeline` is the same ABC over the Claude Messages API,
which carries text and images only: local VAD decides when a turn ends,
local STT transcribes it, Claude streams text and `tool_use` blocks, and
local TTS speaks the sentences as they arrive. Because the seam is the ABC
and not the transport, the mind, the injector, the barge-in controller and
the session manager cannot tell which one is running; what changes is where
the conversation lives (a server-side session versus a message list the
provider owns) and the latency budget (`docs/voice.md`). A persona picks one
with `voice.provider`, and passes it provider-specific settings through
`voice.options` (contracts v1.4).

**Perception (WS3)** runs `python -m asimoov.perception` and talks to the
hub with a `BusClient`, publishing `percept.*` envelopes and exchanging
camera frames as binary `contracts.frames` messages the core never sees.
One process hosts several producers (`perception.face_id`,
`perception.vad`) on one socket, so it publishes through `BusClient.send`
with each envelope's own `src`.

**Faces (WS6)** implement `contracts.face.FaceRenderer`. `start(ctx)`
receives `{"bus", "host", "port", "token", "on_estop", "on_frame",
"clock"}`. `on_frame(topic, ts_ms, content_type, jpeg)` puts a camera
frame the page sent up onto the bus, where perception reads it. A renderer that
serves a page also exposes `process_request` (a
`contracts.face.ProcessRequestHook`, mounted on the hub) and `ws_path` plus
`attach(connection, path=...)` (routed by the hub), so the page and the bus
share port 7331.
