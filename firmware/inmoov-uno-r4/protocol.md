# InMoov line protocol v1

One command per line, one reply per command, plain ASCII. The same parser
serves both links: USB serial at **115200 8N1** and TCP on **port 5005**
(`WiFiServer`). A reply always goes back to the link the command came from,
so a serial monitor and the runtime can both be connected without stealing
each other's answers.

- Lines end with `\n`; a trailing `\r` is ignored. Maximum 160 characters:
  a longer line is dropped whole, tail included, and answered once with
  `ERR line too long`. Its tail is never parsed as a second command, so the
  one-reply-per-line pairing holds even on overflow.
- Commands are case-insensitive, arguments are separated by spaces.
- Anything left after a command's arguments is a syntax error. No command
  ignores a tail: `K now`, `V please` and `E 12 fast` are `ERR bad args`,
  `F happy now` is `ERR unknown emotion` (the emotion is the whole argument).
- Angles are integers in **degrees**, durations integers in **milliseconds**.

## Replies

| Terminator | Meaning |
|---|---|
| `OK` | command accepted |
| `ERR <msg>` | command refused, nothing changed |
| `END` | end of a multi-line answer (`?`, `V`) |

Lines starting with `#` are unsolicited notices (boot banner, watchdog) and
carry no reply semantics: a client must ignore them. A notice goes to every
connected link, serial and TCP alike.

Errors: `ERR unknown command`, `ERR unknown channel`, `ERR bad args`,
`ERR estop`, `ERR no jaw`, `ERR unknown emotion`, `ERR too many channels`,
`ERR line too long`.

## Commands

```
?                             state of every channel, then END
V                             version + channel table, then END
E <id|all>                    attach (and clear the E-STOP latch)
D <id|all>                    detach
P <id> <deg>                  declare the physical position, without moving
S <id> <deg>                  immediate target
M <id>:<deg>[,<id>:<deg>] T<ms>   interpolated multi-servo move (one keyframe)
L <id> <min> <max>            tighten the limits
J <0-100>                     jaw opening, interpolated over 40 ms
F <emotion>                   12x8 LED matrix
K                             keepalive
!                             emergency stop
```

### `?`

```
ST <id> <name> <deg> <min> <max> <attached> <moving>
END
```

`min`/`max` are the limits in force (hard limits of `config.h`, tightened by
`L`), `attached` and `moving` are `0` or `1`.

### `V`

```
V asimoov-inmoov <fw_version> <protocol_version> <board> <channel_count>
CH <id> <name> <min> <max> <rest>
END
```

The adapter builds the body manifest from this answer: `fingers_r` gives
`gesture.hand_right`, `neck_yaw` gives `gaze.pan_tilt`, `jaw` gives
`face.jaw`, and so on.

### `E` / `D`

`E` writes the pulse width **before** `attach()`, so the servo holds the
declared angle instead of jumping to mid-travel. Nothing is attached at boot:
the bust stays limp until an explicit `E`. `E` also clears the E-STOP latch,
which is how a runtime re-arms after a `!`.

### `P`

Recalibrates the internal reference without emitting anything: use it after
moving a joint by hand, before `E`.

### `S` / `M`

`S` jumps to the target on the next 50 Hz tick. `M` interpolates every listed
channel from its current position to the target over `T` milliseconds with an
ease-in-out profile (`u²(3−2u)`), without ever blocking `loop()`. A new `M` on
a moving channel restarts the interpolation from the current position. The `T`
marker is case-insensitive, like the command letter. `T0` is accepted and
behaves like `S`, which is how a runtime stops a move without detaching:
`M <id>:<current> T0` freezes the channel where it stands. Maximum duration:
20000 ms.

Both are refused with `ERR estop` while the E-STOP latch is set.

### `L`

Can only **tighten**: `min` is raised to at least `config.h`'s `min_deg` and
`max` lowered to at most `max_deg`. Widening a range is a physical decision
and only happens by editing `config.h` and re-flashing.

### `J`

`J 0` closes the jaw (its `min`), `J 100` opens it fully (its `max`), moving
over 40 ms. `ERR no jaw` if no channel is named `jaw`.

### `F`

Accepts the nine emotions of the ASIMOOV vocabulary: `neutral`, `happy`,
`excited`, `curious`, `annoyed`, `sad`, `angry`, `love`, `sleeping`.
`annoyed` shares the angry bitmap. The matrix is only redrawn when the
bitmap actually changes, because refreshing it can make a servo twitch.

### `K`

Does nothing but reset the watchdog. The adapter sends one every second.

### `!`

Freezes every channel where it is, latches the E-STOP, replies `OK`
immediately, then detaches everything 300 ms later. `S`, `M` and `J` return
`ERR estop` until an `E`.

## Watchdog

Any received line resets the timer, except one refused for being too long.
It only acts while at least one channel is attached.

| Silence | Action |
|---|---|
| 2 s | HOLD: targets frozen at the current position, servos still attached (`# watchdog hold`) |
| 15 s | everything detached (`# watchdog detach`) |

## Example session (bench, one finger)

```
> V
< V asimoov-inmoov 1.0.0 1 uno_r4_wifi 1
< CH 100 finger_demo 2 108 2
< END
> P 100 2
< OK
> E 100
< OK
> M 100:90 T800
< OK
> K
< OK
> !
< OK
```
