# Voice (WS2)

Speech-to-speech over the OpenAI Realtime API, the audio devices around it, and
the two mechanisms that keep a long conversation usable: barge-in and session
renewal. Everything here implements the frozen contracts
(`contracts/voice.py`, `contracts/audio.py`). `voice/` depends on three
leaf modules of the core and nothing else -- `core/timeouts.py`,
`core/session.py` (which owns the renewal policy) and `core/config.py`
(which owns where `~/.asimoov` is) -- so it never reaches the runtime, the
bus or the mind. The wiring that does is `core/voice_loop.py`, below.

| Module | What it is |
|---|---|
| `voice/openai_realtime.py` | `VoiceProvider` for the Realtime API (asyncio `websockets`) |
| `voice/session.py` | `SessionManager`: truncation, hot renewal, idle disconnect |
| `voice/barge_in.py` | local VAD + the interrupt sequence |
| `voice/vad_silero.py` | Silero ONNX wrapper (extra `[vad]`) |
| `voice/audio/tracker.py` | `HeadTracker`, `StreamPlaybackTracker`, `MicGate` |
| `voice/audio/capture_desktop.py`, `playback_desktop.py` | `sounddevice` devices |
| `voice/audio/capture_android.py`, `playback_android.py` | jnius devices, AEC |
| `voice/audio/resample.py` | linear PCM16 resampling |

## Provider

```python
provider = OpenAIRealtimeProvider(
    api_key=None,              # the core passes core.config.Secrets("openai_api_key")
    url="wss://api.openai.com/v1/realtime",
    on_timestamp=None,         # (name, unix_s): speech_started_ts, speech_stopped_ts, first_audio_delta_ts
    on_assistant_text=None,    # (text): assistant transcript of a finished response
    on_usage=None,             # (usage dict of response.done) -> SessionManager.note_usage
)
await provider.start(events, config)
```

`start` returns immediately; the socket lives in the task
`voice.openai_realtime` with exponential backoff up to 8 s. `config` is the
persona's `voice` section plus what the core assembles:

| Key | Default | Notes |
|---|---|---|
| `model` | `gpt-realtime-1.5` | sent as the `model` query parameter |
| `voice` | `alloy` | |
| `instructions` | `""` | persona system prompt |
| `tools` | `()` | `ToolSpec` objects, converted to Realtime function tools |
| `tool_choice` | `auto` | |
| `turn_detection` | `server_vad` | or `semantic_vad` |
| `vad_threshold`, `silence_duration_ms` | `0.5`, `600` | `server_vad` only |
| `eagerness` | `auto` | `semantic_vad` only |
| `noise_reduction` | `near_field` | `far_field` or `None` |
| `transcription_model` | `gpt-4o-mini-transcribe` | `None` disables transcription |
| `transcription_language` | — | also the `Utterance.lang` published |
| `max_output_tokens` | — | Realtime `session.max_output_tokens` |

Beyond the ABC, the provider exposes what the core and the session manager
need: `active_response_id`, `current_item_id`, `has_pending_tool_call`,
`item_ids`, `is_ready`, `wait_ready()`, `send_tool_result(call_id, result)`,
`request_summary(instructions)`, `delete_item(item_id)`,
`prune_items(max_items)`, `dropped_audio_chunks`.

## Wiring (what the core actually runs)

`core/voice_loop.py` is that wiring, owned by `Runtime` and started by
`Runtime._start_voice`. It is the only place microphone audio is used.

Which devices open is decided by `plan_audio(robot.yaml: audio, body)`:

| `audio.backend` | Devices |
|---|---|
| `auto` (default) | the body's own `audio_source()`/`audio_sink()` if it has both, else the host |
| `body` | the body's devices, or nothing (an error, not a silent fallback) |
| `desktop` | `DesktopAudioSource`/`DesktopAudioSink` (`sounddevice`) |
| `android` | `AndroidAudioSource`/`AndroidAudioSink` (jnius, `VOICE_COMMUNICATION` + AEC) |
| `none` | audio off on purpose |

`audio.source` and `audio.sink` name a device (`default` means the system
default). When nothing can be opened, `AudioPlan.ready` is False and
`asimoov doctor <robot>` prints `audio NOT READY: ...` **and exits 1**: a
robot that can neither hear nor speak is not a healthy robot.

`VoiceLoop` then runs exactly this:

```python
async def handle_chunk(self, pcm16: bytes) -> None:
    if self.tracker.is_playing():
        await self.barge_in.feed(pcm16)   # the only use of the mic while speaking
        return
    if not self.gate.is_open():           # is_playing() + 250 ms tail
        return
    await self.provider.send_audio(pcm16)
```

`VoiceEvents.on_audio_out(item_id, pcm16)` goes to `Runtime.play_audio`, i.e.
`sink.play(item_id, pcm16)`; the sink's `PlaybackTracker` pushes
`energy_at_head` into `FaceState.lip` and `first_audio_played_ts` into the
turn span. `on_tool_call` runs the handler (with its `ToolSpec.timeout_s`)
and answers with `await provider.send_tool_result(call_id, result)` — the
real `ToolResult`, never a literal `"ok"`. The provider is the **single
owner** of the follow-up `response.create`, and only sends it when no
response is active; the core never adds one of its own.

`Runtime` also owns a `SessionManager` whenever the provider is renewable
(`request_summary`, `wait_ready`, `prune_items`), ticked once a second under
the supervised task `voice.session`. On a renewal the runtime repoints the
loop and the barge-in controller at the new provider, writes the summary as
an episode, and reloads the memories in the prompt.

`VoiceEvents.on_response_started(response_id)` (v1.3) is what keeps this
honest: the model starts most responses itself, and without it the core
believes it is idle and injects a perception update into the middle of the
robot's speech.

The provider never gates the microphone itself (the contract says the caller
does) and never queues injections: `inject_system_text` is called by the core's
injector, which coalesces, budgets and flushes at `response.done`.

Two optional contract members (v1.2) carry the rest:
`VoiceProvider.send_tool_result` is the one above, and
`VoiceEvents.on_assistant_text(text)` is called with the assistant
transcript of a finished response, which the core journals (`kind="said"`)
and appends to the transcript an episode summary is built from. The
provider also still accepts an `on_assistant_text=` constructor argument,
for a caller that builds it by hand.

Every bounded wait inside the provider goes through
`core.timeouts.run_with_timeout` rather than `asyncio.wait_for`: on CPython
3.11+ `wait_for` can surface an outer cancellation as `TimeoutError`, and a
supervised loop that catches `TimeoutError` would then swallow its own
shutdown and keep running.

## Audio and gating

PCM16 mono, 24 kHz both ways, as in `contracts/audio.py`.
`StreamPlaybackTracker` is fed by the output callback and derives the audible
head from `consumed - output_latency`, so `remaining_ms`, `played_ms(item_id)`
and `energy_at_head()` describe what is coming out of the speaker, not what has
been written. The mic gate stays closed while `is_playing()` and for 250 ms
after the last audible frame.

Devices run at 24 kHz when they can; otherwise they open at the device rate and
resample (`resample.py`), keeping `sample_rate_hz` at the declared rate.
Android capture uses `VOICE_COMMUNICATION` with the platform
`AcousticEchoCanceler`, and playback uses `USAGE_VOICE_COMMUNICATION` so the
canceler has a reference signal. There is no home-made spectral filter.

## Barge-in

`BargeInDetector` picks Silero (`onnxruntime` + a model in
`$ASIMOOV_HOME/models/silero_vad.onnx` or `$ASIMOOV_SILERO_MODEL`) when both are
present, otherwise a relative-energy detector (RMS above `k = 3` times the
running noise floor for at least 300 ms). `capabilities()` reports
`{"mode": "full_duplex" | "half_duplex", "vad": ..., "reason": ...}` for
`asimoov doctor`; `BargeInConfig(enabled=False)` forces half-duplex.

When it fires during playback, `BargeInController` flushes the tracker, sends
`response.cancel` **with** the `response_id` (skipped while a local function
call is executing, so an action is never killed by an interruption), truncates
the item at the real `played_ms`, and ungates the mic.

## Session

`SessionManager` is ticked about once a second by the core. The four
thresholds are not decided here: they come from `core/session.py`
(`SessionThresholds`, `SessionState`), which owns the policy and is where it
is tested. It prunes conversation items beyond 40 (keeping the first system
item), disconnects after 10 minutes without activity, and renews the session
above 24 000 tokens or 25 minutes of age:

1. at the next silence, an out-of-band summary on the live socket
   (`response.create` with `conversation: "none"`, text output) — it never
   enters the conversation, and it is never asked for mid-sentence. It is
   attributed by the `metadata` tag the API echoes back, never by the order
   `response.created` events happen to arrive in;
2. `on_summary(text)` so the core writes an episode;
3. a second provider is started with the summary appended to the instructions
   (plus `context_provider()` scene text, if given) and waited on until ready;
4. the switch happens at the next silence (`is_silent()`), and only then is the
   old socket closed. Both sockets share the same `VoiceEvents`, so no audio
   window is ever unserved.

A failed renewal leaves the old session running and is retried 60 s later.

## Telemetry

The provider's `on_timestamp` emits `speech_started_ts`, `speech_stopped_ts`
and `first_audio_delta_ts`; the tracker's `set_timestamp_callback` emits
`first_audio_played_ts` the first time an item is audible at the real head.
Both use the same `(name, unix_s)` signature, so the core can pass the same
function. `SessionManager.renewals` feeds
`session_renewals`, `BargeInController(on_barge_in=...)` feeds
`barge_in_count`, and `provider.dropped_audio_chunks` counts mic audio lost to
reconnections.

## Neon bugs this module does not reproduce

| Neon | Here |
|---|---|
| Mic reopened 200 ms after the logical end of playback | `MicGate` on the real head plus a 250 ms tail |
| `AudioSource.MIC`, no AEC, home-made spectral gate | `VOICE_COMMUNICATION` + `AcousticEchoCanceler` |
| Mic fully deaf while speaking, no barge-in | local VAD during playback, `truncate` at `played_ms` |
| `response.cancel` without a `response_id` | `cancel_response` requires one (`ValueError` otherwise) |
| French keyword filter on transcripts | deleted; every transcript is published |
| `str(result)` tool output and an unconditional `response.create` | real `ToolResult` JSON, `response.create` only when idle |
| Session never truncated nor renewed | `SessionManager` |
| Blocking `ws.connect` on the UI thread | one asyncio task, `start` returns immediately |

## Tests

`tests/voice/` runs a local `websockets` server (`fake_realtime_server.py`) —
no network, no API key. It proves the tool-call round-trip, the cancel id, the
barge-in truncation, renewal without a gap, and that not a single
`input_audio_buffer.append` is sent while `remaining_ms() > 0`.

The joint with the core has its own suite,
`tests/integration/test_runtime_openai.py`: a whole `Runtime` on top of the
real provider and the same fake server, which refuses a second concurrent
`response.create` with the API's `conversation_already_has_active_response`.
It covers the session starting with real `ToolSpec`s, an injection held
until a model-initiated response ends, one `response.create` after a tool
call, the barge-in truncation and a full session renewal.
`tests/core/test_voice_loop.py` covers the device plan and the gating.
