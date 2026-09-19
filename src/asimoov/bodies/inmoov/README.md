# bodies/inmoov (WS5)

InMoov adapter: `link.py` (serial/TCP line protocol matching
`firmware/inmoov-uno-r4/`), `gestures.py` (YAML keyframes), `gaze_loop.py`,
`jaw.py`, `manifest.yaml` built dynamically from the firmware `V` response.

Owned by WS5. Implements `asimoov.contracts.body.Body`. See `plan.md`
section 4.9 and `firmware/inmoov-uno-r4/protocol.md` (owned by WS5).
