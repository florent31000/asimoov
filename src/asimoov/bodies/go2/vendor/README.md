# Vendored: `unitree_webrtc_connect`

WebRTC driver for the Unitree Go2 (data channel, SDP exchange with the robot,
DTLS/ICE monkey-patches needed on Android).

| | |
|---|---|
| Upstream | https://github.com/legion1581/unitree_webrtc_connect (PyPI `unitree-webrtc-connect`) |
| Author | legion1581 / Konstantin Severov |
| License | MIT, see `unitree_webrtc_connect/LICENSE` |
| Vendored from | the copy shipped in the Neon prototype (`neon/src/vendor/`), itself taken from upstream >= 2.0.4 |

Vendored rather than depended on because the Android build (buildozer /
python-for-android) needs the monkey-patches in
`unitree_webrtc_connect/__init__.py` (aioice fixed ICE credentials, DTLS
method on old pyOpenSSL, aiortc >= 1.10 SHA digest list) applied before
`aiortc` is used, and because upstream has no API stability guarantee.

The copy Neon shipped had no `LICENSE` file; `unitree_webrtc_connect/LICENSE`
was restored verbatim from upstream. Every source file is otherwise
byte-identical to Neon's copy except for the patches listed below.

## Not vendored

`lidar/` (voxel map decoders). ASIMOOV does not use the Go2 lidar, the
decoders pull `wasmtime` + `lz4`, and Neon's `buildozer.spec` already excluded
them from the APK. `webrtc_datachannel.set_decoder` falls back to a no-op
decoder when the subpackage is missing. Those two files carried a BSD-2-Clause
notice whose copyright line had already been stripped upstream-side; if the
lidar is ever needed, re-vendor them from upstream **with** the full notice.

## Local patches (all marked `# ASIMOOV PATCH`)

| File | Patch | Why |
|---|---|---|
| `msgs/future_resolver.py` | `cancel_resolve(message_type, topic, identifier)` | a request that timed out left its future in `pending_callbacks` forever (plan.md §2 bug 7) |
| `msgs/pub_sub.py` | `cancel_request(topic, identifier)` | public counterpart used by `go2/sport.py` after `asyncio.wait_for` fires |
| `msgs/pub_sub.py` | `asyncio.get_running_loop()` instead of `get_event_loop()` | deprecated API, wrong loop if ever called off-loop |
| `webrtc_driver.py` | `await asyncio.to_thread(...)` around `discover_ip_sn`, `send_sdp_to_local_peer`, `send_sdp_to_remote_peer` | these are blocking `socket` / `requests` calls (up to 3 x 10 s) run from a coroutine; they froze the event loop and made the caller's `asyncio.wait_for(connect(), 20)` useless (plan.md §2 bug 9) |
| `msgs/__init__.py` | added (empty) | `setuptools.packages.find` skips directories without `__init__.py`, so `msgs/` would be missing from the wheel |

Keep this table in sync when re-vendoring: re-apply the patches, do not
rewrite the upstream files.
