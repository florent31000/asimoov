# Perception

The perception process is a separate OS process. It opens a camera, finds and
tracks faces, matches them against the enrolled gallery, and publishes
`percept.person_seen` / `percept.person_lost` to the hub. It can run on the
machine hosting the core, or on any other machine on the LAN.

```
python -m asimoov.perception --hub ws://127.0.0.1:7331 --config robots/avatar/robot.yaml
```

Requires the `[vision]` extra (`pip install asimoov[vision]`: onnxruntime +
opencv-python-headless) and the ONNX models below.

## Privacy by construction

- Frames are decoded, measured, and dropped. Nothing writes an image to disk,
  not even on error paths, not even during enrolment.
- The process never publishes a frame. It only *consumes* the hub's binary
  `frame.*` topic when the camera source is `ws_frames`.
- Embeddings go to SQLite and stay there. Percepts carry a `person_id`, a
  name, a bearing and a confidence — never a vector, never a pixel.
- Nothing in this process talks to an LLM or to any network host other than
  the hub.

## Pipeline

```
camera (5 fps) -> SCRFD-500M -> IoU tracker -> [1 Hz] align + MobileFaceNet
               -> gallery cosine match -> smoothing over 3 -> percept
```

| Stage | Detail |
|---|---|
| Camera | 5 fps, latest-frame-wins for hub-fed sources |
| Detector | SCRFD-500M, letterboxed to 640x640, score >= 0.5, NMS 0.4 |
| Tracker | greedy IoU >= 0.3; a track survives 1 s without a detection |
| Embedding | once per track when it appears, then once per second |
| Alignment | 5-point Umeyama similarity transform onto the 112x112 ArcFace template |
| Embedder | MobileFaceNet w600k, 512-d, L2-normalized |
| Matching | cosine; < 0.45 unknown, 0.45-0.6 uncertain, > 0.6 identified |
| Smoothing | rolling average of the last 3 comparisons of the track |

`person_seen` is published for every visible track on every frame;
`person_lost` once, when the 1 s hysteresis expires.

### Percept fields

- `track_id` — `t1`, `t2`, … stable for the life of the track, never reused.
- `person_id` / `name` — filled **only** when the smoothed score is above
  0.6, i.e. when `identity_status` is `identified`.
- `identity_status` (contracts v1.2) — `unknown`, `uncertain` or
  `identified`. A 0.45-0.6 match is `uncertain` and carries
  `candidate_person_id` / `candidate_name`: who the recognizer suspects,
  which the mind may ask about ("are you Sam?") and must never assume.
- `confidence` — the smoothed identity score when a `person_id` is attached,
  otherwise the detector's confidence that a face is there.
- `face_quality` — detector score x resolution x sharpness, in `[0, 1]`
  (see below).
- `bearing` — from the bbox centre and the configured horizontal FOV, using a
  pinhole model. **`az` positive = the robot's own left** (frozen convention,
  `docs/contracts.md`). A forward-facing camera maps the scene's left to the
  image's left, so a face at `cx < 0.5` gives `az > 0`. The vertical FOV is
  derived from the frame's aspect ratio; `el` positive = up.
- `distance_class` — from the face height relative to the frame: `near`
  above 33 %, `medium` above 13 %, `far` below.

### Face quality

`quality = det_score x min(1, bbox_height_px / 96) x min(1, laplacian_var / 120)`

All three factors matter: a confident detection of a small or blurred face is
not a good enrolment sample. Enrolment keeps samples above 0.6, which in
practice means a sharp face roughly 60 px tall or more. When enrolment times
out, the reply carries `best_quality`, so the robot can say something useful
("come a bit closer") instead of a generic failure.

## Camera sources

| Spec | Source |
|---|---|
| `opencv:N` | local device N, read in a worker thread |
| `ws_frames` | any binary `frame.*` message relayed by the hub |
| `browser` | `frame.browser` only (WS6's face page camera) |
| `go2` / `go2_frames` | `frame.go2` only (WS4's Go2 adapter) |

Hub-fed sources keep only the newest frame: a slow pipeline skips frames
rather than building a backlog of stale bearings.

### Binary frame format

Frozen in contracts v1.2 as `contracts/frames.py`; the byte layout is
documented in `docs/contracts.md`. `perception/frames.py` re-exports it, and
the face page and the Go2's front camera encode with the same functions, so
any producer feeds any consumer.

## Enrolment

The core owns the decision; perception owns the collection.

1. The mind calls `remember_person("Sam")` and creates `p_sam`.
2. The core sends `cmd perception.face_id.enroll {track_id, person_id, name?}`.
3. The module collects 5 embeddings of quality > 0.6 from that track, within
   10 s, embedding on **every** frame while enrolment is active.
4. It writes them through the injected `MemoryStore`
   (`upsert_person` + `add_face_embedding`) and replies.

A `person_id` that already exists is enrolled *into*: the new embeddings are
added to that person's gallery entry, and `created_at`, `relationship` and
`notes` are left as they were. That is what answers "yes, that's me, Sam"
after an `uncertain` match, and what lets a second enrolment session add
angles to an existing face.

```json
{"ok": true, "samples": 5, "person_id": "p_sam", "track_id": "t1"}
{"ok": false, "samples": 2, "person_id": "p_sam", "track_id": "t1",
 "reason": "timeout", "best_quality": 0.41}
```

`reason` is `timeout`, `track_lost`, `cancelled`, `unknown track_id`, or the
exception text. The command takes up to 10 s: the caller's `request()` needs a
timeout above that (12 s is a sane value).

### Reloading the gallery

The gallery is held in RAM, so it must be told when the database changed
under it. After the core forgets someone (`delete_person`) or enrols them
elsewhere, it sends:

```json
cmd  perception.face_id.reload_gallery {}
reply {"ok": true, "identities": 3}
```

`identities` is the number of distinct people the gallery now holds. The
module also drops its cached names and the identity of every live track, so
a forgotten person stops being recognized on the very next frame.

The bench equivalent, with no hub and no core:

```
python -m asimoov.perception.enroll --name Sam --camera opencv:0
```

It waits for exactly one face, enrols it, and writes to
`$ASIMOOV_HOME/memory.db`. `asimoov enroll --name Sam` (WS1) calls
`asimoov.perception.enroll.enroll_person` directly.

## Storage

The default store is `perception/store.py`, writing to
`$ASIMOOV_HOME/memory.db` (`~/.asimoov/memory.db` by default) — the same file
WS1's `SqliteMemoryStore` opens. Both run the same
`core/memory/schema.sql`, so whichever process creates the file leaves it in
a shape the other recognizes, and both open it in WAL mode so one can read
while the other writes.

```sql
persons(id, name, created_at, last_seen_at, relationship, notes)
face_embeddings(person_id, model, dim, vec BLOB, quality, created_at)
facts, episodes, journal, mem_fts   -- created too, written only by WS1
```

`face_embeddings.person_id` references `persons(id)` with
`ON DELETE CASCADE`, so an embedding without a person is refused rather than
orphaned. `vec` is raw little-endian float32, `dim` floats long; `model` is
`w600k_mbf`. Facts, episodes, recall and the journal raise
`NotImplementedError` here — they belong to WS1. Any object implementing
`contracts.memory.MemoryStore` plus `reload_gallery()` can be injected
instead.

## VAD and sound direction

`vad_module.py` turns Silero speech probabilities into `speech_started` /
`speech_ended` (3 windows above 0.5 to start, 15 below 0.35 to end, 512
samples at 16 kHz per window). Production turn-taking still comes from the
Realtime server VAD; this module is for replay, benches, and barge-in without
a server VAD. It consumes PCM16 pushed by its caller and never opens an audio
device.

`direction_stub.py` fixes the interface for a future microphone array. V1
always returns `None`; speech is attributed to the only visible person, or to
the person in attention, on the core side.

## Models

Downloaded to `$ASIMOOV_HOME/models/` (`~/.asimoov/models/` by default,
override the directory itself with `ASIMOOV_MODELS_DIR`) and verified by
sha256:

| Model | File | Source |
|---|---|---|
| SCRFD-500M | `det_500m.onnx` (2.5 MB) | InsightFace `buffalo_sc` pack |
| MobileFaceNet w600k | `w600k_mbf.onnx` (13.6 MB) | same pack |
| Silero VAD (optional) | `silero_vad.onnx` (2.3 MB) | silero-vad v5.1.2 |

```
python tools/download_models.py        # same code path as
asimoov doctor --download-models       # asimoov.perception.models.download_models()
```

A checksum mismatch deletes the file and raises; a missing model raises a
`PerceptionError` naming the command to run. Models are never committed
(`.gitignore` excludes `*.onnx`).

## Measured performance

AMD Ryzen AI 7 350 (Windows 11, Python 3.11, onnxruntime 1.30 CPU provider),
640x360 input letterboxed to 640x640:

| Stage | Time |
|---|---|
| SCRFD detection | 24.2 ms (41 fps) |
| 5-point alignment | 0.8 ms |
| MobileFaceNet embedding | 12.0 ms |
| Quality score | 0.2 ms |

End to end, uncapped, one synthetic 640x360 stream: **31 fps** (32 ms/frame,
including the thread hop per frame). With an embedding on every frame
(enrolment) it drops to **27 fps**. The laptop target of >= 15 fps is met with
a factor of two in hand; the camera is capped at 5 fps in normal operation, so
the pipeline is never the bottleneck.

Pi 5 has not been measured here. The plan's estimate is ~60-90 ms detection +
~30 ms per face, i.e. 5-8 fps, comfortably above the >= 4 fps target; it must
be re-measured on the board.

Android has no p4a recipe for onnxruntime, so there is no face recognition on
the phone in V1: the phone publishes `frame.browser` and a PC or a Pi runs
this process.

## Configuration

```yaml
perception:
  face_id:
    enabled: true
    camera: ws_frames      # opencv:0 | ws_frames | browser | go2
    fov_h_deg: 70          # horizontal field of view of that camera
    db: ~/.asimoov/memory.db
  vad:
    enabled: false
hub: {host: 0.0.0.0, port: 7331}
```

An unknown key is an error, not a silent default. `hub.host: 0.0.0.0` (the
core's bind address) is read as `127.0.0.1` by the client. `--hub`,
`--camera` and `--db` override the file.

## Bus protocol

Perception uses `core.bus.BusClient` (there is no second bus client in the
tree any more). It speaks exactly what the hub serves:

- connects to `ws://host:7331/bus?token=…`, the token read from
  `~/.asimoov/token` (the hub creates it and rejects a wrong one with a 401);
- first message is the control message
  `{"type": "hello", "module_id": …, "subscribe": [...]}`, answered by
  `{"type": "welcome", …}`; a later subscription is announced with
  `{"type": "subscribe", "patterns": [...]}`;
- everything else is envelope.v1 JSON; binary messages are `frame.*`. A
  client only receives frames if its patterns cover `frame.`, which
  `subscribe_frames` takes care of;
- replies carry `corr` = the command's `id`, on the command's own topic;
- reconnects with exponential backoff. Publishing while disconnected drops
  the envelope and counts it in `dropped_publishes`: a queue of stale
  bearings is worse than a gap.

Each module publishes under its own `src` (`perception.face_id`,
`perception.vad`) through a `ScopedBus` view of the single socket.

A module that fails to stop is logged with its traceback and recorded in the
process's health, published as a `state` envelope on `perception.health`
(`{module_id, modules, errors}`). Nothing is swallowed.

`tests/perception/test_hub_interop.py` runs the client against the real
`Hub` (handshake, token rejection, percepts, command/reply, and a
`frame.browser` round trip) so any protocol drift fails a test.

## Tests

`pytest tests/perception` — 193 tests, no model download required. Everything
runs on drawn shapes and stubbed detectors/embedders; the handful of tests
that load the real ONNX graphs are skipped when the files are absent. No real
face is committed anywhere in this repository.
