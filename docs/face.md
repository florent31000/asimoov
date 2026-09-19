# Face (WS6)

Two renderers draw the same face from the same `FaceState`: the web canvas
(`asimoov.faces.server.WebFace` + `faces/web/`) and the Kivy widget
(`asimoov.faces.kivy.renderer.KivyFace`). Both read one shared table,
`faces/web/emotions.json`, so a robot with a tablet and a phone shows the
same eyes.

## Running it

```bash
python -m asimoov.faces.web --demo            # http://127.0.0.1:7331/face
python -m asimoov.faces.web --demo --port 7399 --token abc123
python -m asimoov.faces.kivy.app --demo       # needs pip install 'asimoov[kivy]'
```

`--demo` plays the scripted sequence of `faces.server.demo_state`: the nine
emotions, three seconds each, with a drifting gaze, a blink every four
seconds and a talking burst.

## HTTP and WebSocket paths

| Path | Served by | Content |
|---|---|---|
| `GET /face` | hook | 301 to `/face/` (keeps the query string) |
| `GET /face/` | hook | `index.html` |
| `GET /face/face.js`, `/face/face.css`, `/face/emotions.json` | hook | assets |
| `ws://host:7331/face/ws?token=…` | `WebFace.attach` | the page's connection |

The page and the socket use different paths on purpose: `ProcessRequestHook`
receives a path only, so `GET /face?token=…` from a browser and a WebSocket
handshake on `/face?token=…` would be indistinguishable. Everything still
lives on the single port 7331.

Query parameters of the page: `?token=` (passed on to the socket),
`?theme=light` (cream background, ink outline — the website palette),
`?demo=1` (no socket: the page animates itself and follows the cursor),
`?hud=1` (debug HUD visible on load).

## Wiring it into the hub (WS1)

Nothing to wire by hand: `WebFace` exposes the three optional attributes
`contracts.face` documents (`process_request`, `ws_path`, `attach`) on the
instance, and `Runtime._mount_face` reads them with `getattr` and mounts
them on the hub. Resolving `--face web` is all it takes.

```python
face = WebFace()                       # a FaceRenderer like any other
await face.start({
    "bus": bus,                        # percept.touched on a tap, safety.estop on E-STOP
    "on_estop": safety_guard.trigger,  # fallback when there is no bus (standalone)
    "on_frame": perception.on_frame,   # optional, see "Camera frames"
    "token": token,                    # the ~/.asimoov/token value
})
await face.render(state)               # <= 25 Hz, broadcast throttled to 20 Hz
```

The E-STOP button publishes `TOPICS.SAFETY_ESTOP` when the renderer has a
bus, and falls back to `on_estop` when it does not (a `WebFace` served
standalone by `serve_face`). One path, so a press never stops the robot
twice.

`make_process_request()` returns the `contracts.face.ProcessRequestHook`:
give it the request path, send back the `ProcessRequestResponse` it returns,
or continue the WebSocket handshake when it returns None. `WebFace.render`
never blocks: it coalesces bursts and always flushes the latest state within
50 ms, except a state carrying `blink`, which is sent immediately because a
blink is an edge and not a level.

Messages the page sends:

| Message | Effect |
|---|---|
| `{"type":"tap","intensity":1}` | `percept.touched` `{where:"screen", intensity}` on the bus |
| `{"type":"estop","reason":"face_page"}` | calls `on_estop(reason)` |
| binary | a camera frame, see below |

Without `on_estop`, an E-STOP press is logged at ERROR and nothing else
happens: wire it.

`WebFace.send_metric(topic, data)` pushes a `metric.*` envelope to the pages;
the debug HUD prints whatever it receives.

## Camera frames (`frame.browser`)

The camera button (off by default) captures 640x360 JPEG at 5 fps and sends
each frame as one binary WebSocket message, using the frozen codec of
`contracts/frames.py` — the same one the Go2's front camera and perception's
`ws_frames` source use, so the page can feed the recognition pipeline
without any conversion. The byte layout is documented in
`docs/contracts.md`; `frameHeader()` in `face.js` is the browser side.

`on_frame(topic, ts_ms, content_type, jpeg)` receives every decoded frame;
without it, frames are dropped and counted in `dropped_frame_count` rather
than silently discarded. `ts_ms` is the codec's capture time (Unix
milliseconds modulo 2**32).

## Eyes

Procedural, 60 fps canvas, no image asset (plan.md section 2: Neon decoded a
1 MB PNG per emotion). `emotions.json` holds, per emotion, the parameters
ported from Neon's `EYE_PARAMS` -- `pupil`, `lid_top`, `lid_bot`, `brow`,
`px`, `py` -- plus one iris colour per theme, and the two theme palettes.
Intensity blends an emotion with `neutral`; emotion changes cross-fade over
300 ms with a smoothstep; the gaze follows `FaceState.gaze` with an 80 ms
time constant; `lip` drives the mouth bar with 50 ms smoothing; a rising
edge on `blink` closes the lids for 150 ms; `talking` brightens the iris
glow.

Sign convention (frozen, `docs/contracts.md`): `gaze.x > 0` means the face
looks toward the robot's own left, which is the **viewer's right** on
screen, so the pupils move toward larger canvas x. `gaze.y > 0` is up.

Unlike Neon, the per-emotion offset `px` is *not* mirrored between the two
eyes: mirroring made `curious` look wall-eyed.

## Kivy renderer

`KivyFace` owns an `EyesWidget` and a `queue.Queue`. `render` only enqueues
(thread-safe, drops the oldest state when full); a Kivy `Clock` interval
drains the queue at 30 Hz on the UI thread and redraws. Kivy is an optional
extra: importing `asimoov.faces.kivy.renderer` works without it, and
instantiating `KivyFace` raises `ImportError: … pip install 'asimoov[kivy]'`.

## Embedding the eyes on the website (WS7)

Copy `src/asimoov/faces/web/{face.js,face.css,emotions.json}` next to each
other under the site's static assets, and in the hero:

```html
<canvas id="face"></canvas>
<script src="/eyes/face.js"></script>
```

`face.js` resolves `emotions.json` relative to its own `src`, so any mount
path works. Load the page (or iframe) with `?theme=light&demo=1`: `demo=1`
means no WebSocket -- the eyes cycle through the emotions, blink, and follow
the cursor. To drive them from the site's own script instead, leave `demo`
out and call `window.asimoovFace.render({emotion, intensity, gaze:{x,y},
lip, blink, eyelids, talking})`. `index.html` also carries the E-STOP,
camera and HUD controls; a hero embed usually copies only the canvas.

## Avatar body

`asimoov.bodies.avatar.AvatarBody` is the virtual body of
`robots/avatar/`: capabilities `face.screen`, `face.eyelids`, `audio.in`,
`audio.out`, `gesture.nod`, `gesture.shake_head`. `nod` and `shake_head`
are gaze animations (900 ms), `express` sets the emotion, `look_at` maps
degrees to a normalized gaze (45 deg azimuth and 30 deg elevation map to
±1, per `manifest.yaml: limits`). `move` returns `unsupported`.
`audio_source()` / `audio_sink()` return None so the core uses the host
devices from `robot.yaml: audio`, while the manifest still declares the
audio capabilities.

The Mind owns `face.state`. While an animation or a gaze move is running,
the avatar publishes only its contribution — the gaze it is showing — on
`face.overlay`, and the Mind mixes it on top of its own face for 0.25 s.
The overlay lapses on its own when the animation stops.
