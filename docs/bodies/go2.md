# Unitree Go2

`asimoov.bodies.go2.Go2Body` drives a Unitree Go2 over WebRTC. Install the
extra: `pip install asimoov[go2]`.

```
src/asimoov/bodies/go2/
├── adapter.py           Go2Body: the contracts.body.Body implementation
├── motion.py            MotionController: the single joystick writer
├── sport.py             sport commands with a real timeout, ACTION_MAP
├── frames.py            front camera -> frame.go2 (optional)
├── android_network.py   Wi-Fi/cellular routing, AP mode on Android only
├── manifest.yaml        body.v1 manifest (capabilities, limits, safety)
└── vendor/              unitree_webrtc_connect (MIT, see vendor/README.md)
```

## Configuration

`robots/go2/robot.yaml`:

```yaml
body:
  type: go2
  config:
    mode: sta                  # sta (default) | ap | remote
    serial_number: ""          # STA: found by multicast when robot_ip is empty
    robot_ip: ""               # STA: skips discovery, e.g. 192.168.1.42
    obstacle_avoidance: true
    frames: { enabled: false, fps: 5 }
```

`mode: sta` is the default and the recommended setup: the Go2 joins the
house Wi-Fi, the machine running ASIMOOV stays on the same network, and no
network rebinding is needed. `mode: ap` connects to the robot's own access
point (192.168.12.1); on Android that means the phone has no internet on
Wi-Fi, so `android_network` binds the process to Wi-Fi for the connection
and unbinds immediately after. `mode: remote` goes through Unitree's relay
and needs an account; it is untested.

### Putting the Go2 on the house Wi-Fi (STA)

1. Power the robot on and wait for it to stand or for the head LED to settle.
2. In the Unitree Go app, connect to the robot (Bluetooth then its AP).
3. `Device` -> `Network` -> `WLAN`, pick the 2.4 GHz house SSID, enter the
   password. The robot reboots on the house network.
4. Note the serial number under the robot's chin (also shown in the app).
   Put it in `serial_number`, leave `robot_ip` empty: ASIMOOV finds the
   robot with the same multicast query the app uses.
5. If discovery fails (multicast is often filtered on guest/mesh Wi-Fi),
   find the lease in the router's DHCP table and set `robot_ip` instead.

Only one WebRTC client at a time: the robot answers `reject` while the
mobile app is connected. Close the app before starting ASIMOOV.

## What the adapter guarantees

| Contract | How |
|---|---|
| `start()` returns fast | it spawns `_connect_loop` and waits at most 50 ms for the first attempt; failures land in `health().errors`, never as an exception |
| retries | `run_with_timeout(connect, 20 s)` then a `[5, 10, 30, 60]` s backoff, repeating 60 s |
| `gesture()` never lies | it awaits the robot's own reply; no reply within `timeout_s` gives `BehaviorResult.timeout()`, a non-zero status code gives `error` |
| `move()` is `long` | returns `started` with the effective `eta_s`, then publishes the `MotionController`'s real outcome on `body.reply` (`ok`, `interrupted` if it was stopped or replaced, `error` with a reason) |
| `stop_all()` < 200 ms | zeroes the joystick synchronously and fires `StopMove` without awaiting it |
| disconnection stops the robot | `_link_monitor` polls the peer connection every second; a drop calls `stop_all` then restarts the connect loop (`safety.stop_on_disconnect: true`) |
| flips are impossible | `safety.forbidden` (flips, Handstand, FrontJump, FrontPounce) is enforced in `sport.py`, before anything is sent |

### Motion

Everything that moves goes through one `MotionController._sender` task at
10 Hz, writing one `JoystickState` to `rt/wirelesscontroller`. Emulating
the remote rather than sending the `Move` sport command is deliberate: it
is the only path that keeps the Go2's own obstacle avoidance in the loop.

- `move()` / `turn()` **replace** the target; there is never a second task
  fighting the first.
- A timed motion carries a deadline, capped by `limits.max_continuous_motion_s`.
- A motion with `duration_s <= 0` expires after `safety.watchdog_ms`
  unless `keepalive()` refreshes it, so a dead commander stops the robot.
- If the sender loop itself stalls for more than the watchdog (suspended
  process, starved event loop), the target is dropped instead of resumed.
- `stop_all` zeroes the joystick, sends three zero frames, and fires
  `StopMove`.

### Gaze (`look_at`)

The Go2 has no neck: gaze is body yaw. `gaze_yaw_rx(az)` maps the target
azimuth to a joystick yaw, with a dead zone of 8 deg (below that the robot
does not twitch), growing linearly to a maximum of 0.3 at 45 deg.
`GazeTarget.az > 0` is the robot's own left, and the Go2 yaws left for
`rx < 0`, so `rx = -sign(az) * magnitude`. Gaze is only applied when no
motion is commanded, and expires with the same watchdog.

### Percepts

`rt/lf/lowstate` and `rt/lf/sportmodestate` are stored as they arrive and
turned into percepts at 2 Hz, on change only:

- `percept.battery`: `level` = `bms_state.soc / 100`, `charging` =
  `bms_state.current > 0` (to be confirmed on a charging robot).
- `percept.body_state`: `moving` from the reported velocity, `posture`
  from the last posture command the adapter itself sent (the Go2 does not
  report a posture enum).

With `frames.enabled`, the aiortc video track is encoded to JPEG at 5 fps
and published as binary `frame.go2` messages with the frozen codec of
`contracts/frames.py` (PyAV's mjpeg encoder, no Pillow) — the same bytes the
face page sends, so perception decodes both with one function. It needs a
bus that carries frames (`LocalBus`, `BusClient`); with anything else the
camera is disabled and says so in `health().errors`, rather than publishing
JPEGs nobody receives.

## Neon bugs this adapter does not reproduce

Plan sections 2 and 4.8, all covered by `tests/bodies/go2/`:

| Neon | Here |
|---|---|
| `publish_request_new` awaited forever, pending futures leaked | `SportClient.request` = `run_with_timeout` + `cancel_request` on the vendored resolver |
| one task per `move`, one per `turn`, `turn` not interruptible | one `_sender` task, one target, deadlines |
| `max_continuous_walk_seconds` announced but never applied | `limits.max_continuous_motion_s` caps every deadline |
| fabricated tool results ("Performing X" before any reply) | `BehaviorResult` built from the robot's reply or a real timeout, and `body.reply` from the motion's real outcome |
| `ws.connect` blocking the UI thread; blocking HTTP inside a coroutine | `start()` is non-blocking, and the vendored driver's blocking SDP/discovery calls run in `asyncio.to_thread` |
| Android-wide network rebinding for every connection | STA by default, rebinding only in AP mode |
| flips reachable from the LLM | `safety.forbidden` filtered in `sport.py` |
| `RecoveryStand` before every single gesture | sent only when the tracked posture is not already standing |

## Manual test on a real Go2

Needed: a Go2 (tested target: Go2 Air), 3 m x 3 m of clear floor, the
robot on the house Wi-Fi (see above), the mobile app closed. Run each step
and note the result; anything that fails is a bug, not a quirk.

1. **Link** -- `asimoov run robots/go2`. Within ~10 s, `body.health`
   reports `connected: true` and a plausible battery level. Kill the
   robot's Wi-Fi (or power it off): `connected` goes false within ~2 s and
   the log shows the backoff retries.
2. **Motion mode** -- at startup the robot is in normal mode even if the
   app left it in AI mode; the log shows either "already normal" or the
   switch followed by a 2 s settle. The mode is read back after the settle:
   a switch the robot ignored shows up in `health().errors`.
3. **Obstacle avoidance** -- walk the robot toward a wall at 0.5 speed: it
   slows and stops on its own. If it does not, obstacle avoidance was not
   acknowledged; check `health().errors`.
4. **Gestures** -- `wave_hello`, `sit`, `stand_up`, `lie_down`, `stretch`,
   `heart`, `dance`. Each returns `ok` only after the robot actually
   moved. From a sitting posture, the first gesture stands the robot up
   first; a second gesture in a row does not repeat the recovery stand.
5. **Forbidden** -- ask for a flip. The robot must not move and the result
   must be an error naming the manifest, with nothing sent on the wire.
6. **Timed move** -- `move(vx=1, duration_s=3)`: the robot walks forward
   ~3 s, stops by itself, and `body.reply` reports the action done.
7. **Watchdog** -- start a continuous move, then `Ctrl+C` the process: the
   robot stops within 500 ms (this is the one to check on carpet, with a
   hand on the robot).
8. **E-stop** -- while walking, trigger the emergency phrase or the face
   page's stop button: motion stops in well under a second and the robot
   stays standing.
9. **Gaze** -- place a person 40 deg to the robot's **left**: the robot
   rotates **left** (counter-clockwise seen from above). Repeat on the
   right. Stand in front (< 8 deg): the robot must not move at all.
10. **Gaze vs motion** -- during a commanded move, a person to the side
    must not make the robot turn.
11. **Battery** -- compare `percept.battery` with the app's percentage.
    Put the robot on its charger and confirm `charging: true` (this is the
    heuristic that still needs validation).
12. **Camera** -- with `frames.enabled: true`, the bus receives ~5
    `frame.go2` messages per second and a decoded frame looks right.
13. **Endurance** -- leave it connected 30 minutes idle, then command a
    gesture: it must still answer without a reconnect.

## Limitations

- Go2 audio (its microphone and speaker) is V1.1; `audio_source()` and
  `audio_sink()` return `None`.
- The lidar decoders are not vendored (see `vendor/README.md`).
- `mode: remote` is implemented but untested.
- The adapter tracks posture from the commands it sends; a posture changed
  with the physical remote is not reflected until the next command.
