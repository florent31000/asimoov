# bodies/inmoov

`InMoovBody`: the `Body` adapter for an InMoov bust (and for the one-finger
bench) driven by `firmware/inmoov-uno-r4`.

`link.py` (serial / TCP line protocol), `gestures.py` (YAML keyframes ->
`M` commands), `gaze_loop.py`, `jaw.py`, `servo_face.py`, `adapter.py`.

See `docs/bodies/inmoov.md` and `firmware/inmoov-uno-r4/protocol.md`.
