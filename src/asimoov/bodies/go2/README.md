# bodies/go2 (WS4)

Unitree Go2 adapter: `adapter.py` (`Go2Body`), `motion.py` (single 10 Hz
joystick writer), `sport.py` (timeout-safe sport commands, `ACTION_MAP`),
`frames.py` (optional `frame.go2` camera stream), `android_network.py` (AP
mode only), `manifest.yaml`, and `vendor/` (the `unitree_webrtc_connect`
WebRTC driver, MIT -- see `vendor/README.md`).

Documentation, including the manual test procedure on a real robot:
`docs/bodies/go2.md`. Design: plan.md sections 2 and 4.8.
