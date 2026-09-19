# bodies/go2 (WS4)

Unitree Go2 adapter: WebRTC connection (vendored `unitree_webrtc_connect` +
DTLS patches), `MotionController` (single 10 Hz sender task), `sport.py`
(timeout-safe sport commands), `manifest.yaml`, `android_network.py`.

Owned by WS4. Implements `asimoov.contracts.body.Body`. Reference: bugs and
fixes tracked in `plan.md` sections 2 and 4.8 (Neon `ACTION_MAP`,
`publish_request_new` without timeout, concurrent motion tasks, forbidden
flips).
