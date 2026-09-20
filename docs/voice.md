# Voice (WS2)

Two providers behind one frozen ABC: speech-to-speech over the OpenAI
Realtime API, and a Claude pipeline that assembles the same thing out of
local VAD, local STT, the Claude Messages API and local TTS. Around them,
the audio devices and the two mechanisms that keep a long conversation
usable: barge-in and session renewal. Everything here implements the frozen
contracts (`contracts/voice.py`, `contracts/audio.py`). `voice/` depends on
three leaf modules of the core and nothing else -- `core/timeouts.py`,
`core/session.py` (which owns the renewal policy) and `core/config.py`
(which owns where `~/.asimoov` is) -- so it never reaches the runtime, the
bus or the mind. The wiring that does is `core/voice_loop.py`, below.

| Module | What it is |
|---|---|
| `voice/openai_realtime.py` | `VoiceProvider` for the Realtime API (asyncio `websockets`) |
| `voice/claude_pipeline/` | `VoiceProvider` for the Claude Messages API (extra `[claude]`) |
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

## Claude pipeline

The Claude Messages API takes text and images. It has no audio input, no
speech output and no realtime speech-to-speech channel, so there is no
session to open: `ClaudePipelineProvider` builds one locally and presents
exactly the same ABC, which is why `VoiceLoop`, `MicGate`,
`BargeInController` and `SessionManager` run unchanged on top of it.

```
mic PCM16 -> TurnDetector (Silero, energy fallback) -> utterance buffer
                                                          |
                                                     SpeechToText
                                                          |
                     Claude (streaming text + tool_use, adaptive thinking)
                                                          |
                                 sentence chunks -> TextToSpeech -> on_audio_out
```

| Module | What it is |
|---|---|
| `claude_pipeline/provider.py` | the `VoiceProvider`: turns, history, tools, cancel, renewal |
| `claude_pipeline/turns.py` | `TurnDetector`: 300 ms of speech opens a turn, 700 ms of silence closes it |
| `claude_pipeline/stt.py` | `SpeechToText` + `FasterWhisperSTT` (CPU int8) + `FakeSTT` |
| `claude_pipeline/tts.py` | `TextToSpeech` + `KokoroTTS` (24 kHz, ONNX) + `FakeTTS` |

```python
provider = ClaudePipelineProvider(
    api_key=None,       # the core passes core.config.Secrets("anthropic_api_key")
    client=None,        # an anthropic.AsyncAnthropic; tests inject a fake
    stt=None, tts=None, turns=None,   # override what the persona names
    fallbacks=True,     # server-side refusal fallbacks
    on_timestamp=None, on_assistant_text=None, on_usage=None,
)
```

`config` is the persona's `voice` section, with the pipeline's own keys
coming from `voice.options` (contracts v1.4):

| Key | Default | Notes |
|---|---|---|
| `model` | `claude-opus-5` | |
| `voice` | — | the Kokoro voice (`af_heart` en, `ff_siwis` fr) |
| `effort` | `low` | `output_config.effort`; this route is latency-bound |
| `end_silence_ms` | `700` | silence that closes a turn |
| `stt.engine`, `stt.model` | `faster_whisper`, `small` | CPU, int8 |
| `tts.engine` | `kokoro` | `fake` in tests |
| `transcription_language` | — | STT language and `Utterance.lang` |
| `max_output_tokens` | `1024` | a spoken turn is short |

What the request looks like, and why:

- **System prompt**: one cached block (`cache_control: ephemeral`) holding
  the persona prompt plus *"Latency-sensitive; begin your visible answer
  immediately."*, which is what keeps the model from thinking before the
  first token. Nothing volatile goes in it, so the prefix stays cached.
- **Injections** (`inject_system_text`) are buffered and flushed into
  `messages` as a `{"role": "system"}` message on the next turn -- supported
  mid-conversation on `claude-opus-5`, and it leaves the cached prefix
  intact. It cannot be the first message, so a turn the robot starts itself
  (`request_response`) opens on a fixed seed line rather than on a made-up
  human one, with the initiative instruction as a second system block.
- **Thinking** is `{"type": "adaptive"}`; `budget_tokens` is gone on this
  model.
- **Tools** are the registry's `ToolSpec`s, with `eager_input_streaming:
  true` (the default for a streaming request with client tools) and
  `strict: true` only on a schema that can carry it -- a closed object whose
  properties are all required. Eager streaming means the server no longer
  validates the input, so the parsed input is checked against the schema
  here and an invalid one answers `ToolResult(status="error",
  reason="invalid_arguments")` without ever running the tool.
- **`stop_reason == "refusal"`** is read before the content: a decline can
  cut a `tool_use` off mid-input. `fallbacks="default"` (beta
  `server-side-fallback-2026-07-01`) is on, so the API re-runs a declined
  request on the substitute it recommends for that refusal category instead
  of handing back a mute robot.

The turn itself is a manual loop: one streaming request, then one
`tool_result` continuation per round (four at most), never two turns at
once. Text is spoken as it streams -- cut at the first `.!?…:;` past 12
characters, synthesized by a single queue worker so the order holds.
`cancel_response(response_id)` stops the stream and drains that queue;
`truncate_item(item_id, played_ms)` then trims the assistant turn kept in
history by the proportion of the item's audio that actually reached the
speaker (character count, not tokens -- an approximation, and the only one
available without re-aligning audio to text).

`SessionManager` works because the provider also implements
`request_summary` (a text-only request that never enters the history),
`wait_ready` (the STT and TTS models and the Silero session are loaded in a
warm-up task, so `start` still returns well inside the 100 ms the contract
allows) and `prune_items` (which never cuts on a `tool_result`: a history
whose `tool_use` lost its answer is rejected by the API). The provider
reports each finished message's `usage` through its `on_usage` hook;
`Runtime.build` wires it to `SessionManager.note_usage` for both providers
(through `_voice_factory`, which only forwards a hook when the provider's
constructor actually declares it), so renewal triggers on tokens as well as
age. A renewal also starts a second provider: `build_stt` and
`build_tts` hand back the engine already in memory rather than loading a
second Whisper model beside the first.

**Latency.** Three serial costs the Realtime API did not have: the
end-of-turn silence (700 ms, configurable), one STT pass over the whole
utterance (faster-whisper `small`, int8, ~0.3-0.8 s for a short sentence on
a laptop CPU), and the first TTS chunk. Claude's own time to first token at
`effort: low` is in between. Expect roughly 1.5-2.5 s from the end of speech
to the first audible word on a laptop, against ~0.5 s for the Realtime API.
Sentence chunking is what keeps the rest of the answer flowing; the first
chunk is the one that costs.

**Cost.** `claude-opus-5` is $5 / $25 per MTok (input / output). A spoken
turn sends the whole history every time, which is what prompt caching is
for: the persona prompt is a cached prefix, read at ~0.1x, so a turn whose
history is a few thousand tokens costs on the order of a cent, not ten.
Cache reads show up in `usage.cache_read_input_tokens`; if that stays at
zero across turns, something volatile crept into the cached block.

**Engines, verified on PyPI on 2026-09-20.** `anthropic` 1.7.0,
`faster-whisper` 1.2.1, `kokoro-onnx` 0.6.1, `piper-tts` 1.8.0 all exist.
Kokoro-82M ships exactly **one** French voice, `ff_siwis` (female, grade B,
under 11 hours of training data), so Kokoro is the single default for both
languages and Piper is not shipped: one engine, one default per language
(`af_heart` for `en`, `ff_siwis` for `fr`). A language with no documented
default raises rather than guessing a voice. `kokoro-onnx` phonemizes with
`espeak-ng`, which must be installed system-wide (`asimoov doctor` reports
whether it is on `PATH`) and needs `kokoro-v1.0.onnx` + `voices-v1.0.bin`
under `$ASIMOOV_HOME/models` (`$ASIMOOV_KOKORO_MODEL` /
`$ASIMOOV_KOKORO_VOICES` move them) -- `asimoov doctor --download-models`
fetches both, registered in `perception/models.py` alongside the perception
ONNX files since they share the same `models_dir()`. If
`ff_siwis` turns out too thin in practice, `PiperTTS` (`fr_FR-siwis-medium`,
`fr_FR-tom-medium`, `fr_FR-upmc-medium`) is the next engine to add behind
the same `TextToSpeech` protocol; so is ElevenLabs, and Deepgram or
ElevenLabs Scribe behind `SpeechToText`. None of them is implemented.

The key is read through `core.config.Secrets` under `anthropic_api_key`
(`$ASIMOOV_ANTHROPIC_API_KEY`, then `~/.asimoov/secrets.yaml`, then
`$ANTHROPIC_API_KEY`), never logged and never printed. `_voice_factory`
picks the key from the provider class's own `secret_key`, so the Claude
provider is never handed the OpenAI one.

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

`tests/voice/claude_pipeline/` runs the same kind of thing against a
scripted Anthropic client (`tests/voice/fake_anthropic.py`) with `FakeSTT`
and `FakeTTS`: a whole turn from microphone chunks to synthesized audio, the
request's cached system block and beta headers, one tool call producing one
continuation, invalid tool arguments answered without running anything, a
cancel that stops the speech and keeps only what was heard, an injection
buffered mid-turn and delivered as a system message, a refusal, the renewal
summary staying out of the history, and that the key never reaches a log.
`tests/integration/test_runtime_claude.py` is the joint with the core: a
whole `Runtime` on the real provider, replaying the `family_evening`
opening, proving `remember_person` flows through Claude tool use into the
memory store.

The joint with the core has its own suite,
`tests/integration/test_runtime_openai.py`: a whole `Runtime` on top of the
real provider and the same fake server, which refuses a second concurrent
`response.create` with the API's `conversation_already_has_active_response`.
It covers the session starting with real `ToolSpec`s, an injection held
until a model-initiated response ends, one `response.create` after a tool
call, the barge-in truncation and a full session renewal.
`tests/core/test_voice_loop.py` covers the device plan and the gating.
