# InMoov body

An [InMoov](https://inmoov.fr) bust driven by an Arduino UNO R4 WiFi.
Two pieces: the firmware (`firmware/inmoov-uno-r4/`) and the adapter
(`src/asimoov/bodies/inmoov/`), talking the line protocol of
[`protocol.md`](../../firmware/inmoov-uno-r4/protocol.md) over USB serial or
TCP.

The adapter works on a bare bench as well as on a full bust: the manifest is
built from the firmware's `V` answer, so one finger on a pin already gives a
working body with a `gesture.finger_demo` capability.

Because that manifest only exists once the link is up, the core rebuilds
everything derived from it (safety limits, the visible behaviors, the tool
enums) right after `Body.start` returns — see
`Runtime._bind_body_manifest`. A body whose manifest is static pays nothing
for it.

A long gesture reports its outcome on `body.reply` as
`{action_id, name, status, reason}`, the same shape the Go2 and the core's
own executor use; the core turns each one into an `[Action …]` injection.
An interrupted one reports `status: "canceled"`, so the model is never left
waiting on an action that already ended.

## Safety first

- **Never power servos from the Arduino 5 V pin.** One MG996R stalls at more
  than 2 A; the board's regulator dies long before. Servos get their own
  6 V supply (6 A or more for a bust), and the Arduino keeps its USB.
- **Common ground.** The 6 V supply's ground, the PCA9685 ground and the
  Arduino ground must be tied together, or the PWM signal floats and the
  servos jitter.
- **A physical E-stop on the 6 V rail.** A switch or an unplugged barrel jack
  is the only stop that works when the software is the problem. `!`, the
  watchdog and `stop_all` are the second line of defence, not the first.
- **Hard limits live in `config.h`.** The `L` command can only tighten them.
  Widening a range means re-measuring the joint by hand, pulley unscrewed,
  before re-flashing: past the mechanical stop, the tendon breaks the part.
- **Nothing is attached at boot.** The bust stays limp until an explicit `E`,
  so a reset never throws a joint to mid-travel.
- Keep hands and faces out of the arm's reach while testing a gesture for
  the first time, and run it once with the servo rail off.

## Wiring

```
USB  ──────────────► Arduino UNO R4 WiFi
                       │ SDA/SCL (A4/A5) + GND
                       ▼
6 V PSU ──┬──────► PCA9685 #0 (A0 open,  0x40)  channels 0-15
          │            │ chained via the I2C header
          └──────► PCA9685 #1 (A0 bridged, 0x41) channels 16-31
                       │
                       ▼
                  servos (V+ from the 6 V rail, signal from the board)
```

- The two drivers differ only by the `A0` solder bridge: `0x40` and `0x41`.
- The bench servo is the exception: it is wired straight to **pin 3** and
  declared as channel **100** in `config.h` (`finger_demo`).
- The 12x8 LED matrix is on the UNO R4 itself; nothing to wire.

## Flashing

```sh
arduino-cli lib install "Adafruit PWM Servo Driver Library"
arduino-cli compile --fqbn arduino:renesas_uno:unor4wifi firmware/inmoov-uno-r4/asimoov_inmoov
arduino-cli upload  --fqbn arduino:renesas_uno:unor4wifi -p COM5 firmware/inmoov-uno-r4/asimoov_inmoov
```

For the TCP link, copy `secrets.h.example` to `secrets.h`, fill in the
network and set `ASIMOOV_WIFI_ENABLED` to `1`. The board then prints its
address on the serial port at boot and listens on port 5005. `secrets.h` is
git-ignored and never leaves the machine.

## Bench procedure (replaces `pilote-doigt.html`)

Any serial monitor at 115200 does the job, as does a Python session. The web
page of the finger starter is no longer needed: the same commands are typed
by hand, and the adapter speaks them.

1. **Servo rail off**, USB connected, open the serial monitor at 115200 with
   a newline line ending. The board prints
   `# asimoov-inmoov 1.0.0 ready, all servos detached`.
2. `V` lists the channels. `?` shows `ST 100 finger_demo 2 2 108 0 0`:
   detached, no pulse.
3. Move the finger by hand to its rest position and declare it: `P 100 2`.
   Declaring is not moving, nothing is emitted yet.
4. Power the servo rail. `E 100` attaches: the servo holds the declared
   angle, no jump.
5. `M 100:40 T800`, then `M 100:2 T800`. The motion is interpolated and the
   board keeps answering during it.
6. Stop sending anything: after 2 s the board prints `# watchdog hold`,
   after 15 s `# watchdog detach`.
7. `!` at any moment freezes then releases everything; `E 100` re-arms.
8. `F happy`, `F sleeping` exercise the LED matrix.

Then the same thing through the adapter, servo rail on, finger clear:

```python
import asyncio
from asimoov.bodies.inmoov import InMoovBody
from asimoov.contracts.body import BodyContext

async def main():
    body = InMoovBody({"link": "serial", "port": "COM5"})
    await body.start(BodyContext())
    await asyncio.sleep(1)
    print(await body.health(), body.manifest.capabilities)
    print(await body.gesture("finger_demo", {}, timeout_s=5))
    await body.stop()

asyncio.run(main())
```

Without hardware, the same session runs against the protocol simulator
(`tests/bodies/inmoov/fake_firmware.py`), which implements the whole command
set in Python.

## Configuration

`robots/inmoov/robot.yaml`:

```yaml
body:
  type: inmoov
  link: serial      # or tcp
  port: COM5        # serial port, or TCP port with link: tcp
  # host: 192.168.1.50
  limits:           # extra tightening, re-applied at every connect
    jaw: [12, 45]
```

## What the adapter does

- `link.py` -- one command in flight, replies paired in FIFO order, 500 ms
  timeout, `K` every second. A drop triggers a local `stop_all` and the
  reconnect re-reads `V` and re-applies the configured `L` limits, since a
  reconnect usually means the board rebooted on its `config.h` defaults.
  It also holds the E-STOP latch (see below).
- On connect, the adapter attaches `neck_yaw`, `neck_pitch`, `jaw` and
  `eyelids`: the always-on loops drive them but never attach anything, and a
  gesture only attaches its own joints. Without this the head stays limp.
- `gestures.py` -- `gestures/*.yaml` keyframes become `M ...T<dt>`
  sequences, clamped a second time with the limits read from `V`.
- `gaze_loop.py` -- 5 Hz P-controller (Kp 0.6, 3 deg dead zone, 40 deg/s) on
  `neck_yaw`/`neck_pitch`, with a slow scan when no target arrives for 8 s.
  `GazeTarget.az > 0` is the robot's own left and turns the neck left.
- `jaw.py` -- `FaceState.lip` to `J`, only past a 5 % change and at most
  25 times a second.
- `servo_face.py` -- `ServoFace`: emotion to the LED matrix, `eyelids` to
  their servo (0 = wide open, 1 = closed).

Capabilities are derived from the channel names the firmware reports:
`fingers_r` gives `gesture.hand_right`, `fingers_l` `gesture.hand_left`,
`neck_yaw` `gaze.pan_tilt`, `jaw` `face.jaw`, `eyelids` `face.eyelids`; the
LED matrix always gives `face.leds`. A gesture is only exposed when every
joint it names exists.

## Emergency stop

`stop_all` writes `!` ahead of whatever command is in flight, so an E-STOP
never queues behind a running move. The firmware freezes every channel,
refuses `S`, `M` and `J` with `ERR estop`, and detaches 300 ms later. `E` is
what re-arms it. The latch protects against a runaway move, not against an
operator who asks for a new one: `link.py` remembers it and prefixes the
next `S`, `M` or `J` with `E all`, whoever the caller is. Re-arming only on
the next gesture left the gaze, jaw and face loops refused forever.
`BodyHealth.errors` carries `ERR estop` while the latch is set, plus any
loop that has been refused several times in a row. In practice the 5 Hz gaze
loop re-arms the bust within 200 ms of a `stop_all`: the software `!` is a
freeze, the switch on the 6 V rail is the stop.

`!` is reserved for `stop_all`. Interrupting a gesture is not an emergency:
the runner reads `?` and sends `M <ch>:<current> T0` on the channels it was
moving, which stops them where they are with the servos still attached. A
`!` there would detach the arm 300 ms later and drop it.
