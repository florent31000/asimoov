# Android

The same APK plays three roles. Nothing in it is a secret.

## Build

```bash
./android/build.sh              # builds the image, then the debug APK
./android/build.sh --release    # unsigned release build
```

The APK lands in `android/bin/`. The build runs entirely inside the image
defined by `Dockerfile` (Ubuntu 22.04, JDK 17, SDK 35, build-tools 35.0.0,
NDK 25b, buildozer 1.5.0, python-for-android pinned to `v2024.01.21`), so a
laptop and CI produce the same thing. `~/.buildozer` and `~/.gradle` are
docker volumes: the first build takes about an hour, later ones minutes.

To get only the toolchain, without touching the project:

```bash
docker build --target toolchain -t asimoov-android-toolchain android/
```

`build.sh` stages `src/asimoov/` and `robots/avatar/` next to `main.py`
because buildozer packages `source.dir`. Both copies are gitignored, rebuilt
on every run, and the build aborts if a `secrets.yaml` ever appears in them.

## What is in the APK

Core dependencies only (`websockets`, `pyyaml`, `numpy`, `jsonschema`,
`certifi`) plus `kivy` and `pyjnius`, plus the `[go2]` WebRTC stack proven by
Neon. Never `pydantic`, `onnxruntime`, `opencv`, `scipy`, `aiohttp`,
`msgspec`, `orjson`, `uvloop` or `torch` (plan.md 4.10). python-for-android
does not resolve transitive dependencies, which is why `buildozer.spec` lists
`pyee`, `google-crc32c`, `idna`, `typing_extensions` and `packaging`
explicitly.

Drop everything from `aiortc` down in `requirements` for a face-only or
InMoov-only APK; the build gets about three times shorter.

## The three roles

| Role | What the phone is | Needs |
| --- | --- | --- |
| `core` | The whole robot: runtime, voice, memory, face, microphone and speaker. The body, if any, connects to it. | API key |
| `head` | Eyes, microphone, speaker and camera attached to the hub of a core running elsewhere. Audio travels as PCM16 over the bus. | Address of the core |
| `face_only` | Not this APK. `http://<core>:7331/face` in Chrome. No install, no permissions. | Nothing |

`asimoov.android.permissions.ROLE_PERMISSIONS` says what each role asks for at
runtime. `face_only` asks for nothing.

## Settings screen

Three fields, written to `getFilesDir()/secrets.yaml` and nowhere else. That
path is private app storage: it is not in the APK, not in the repository, not
readable by other apps, and it is wiped when the app is uninstalled.

| Field | Stored as | Used by |
| --- | --- | --- |
| API key | `api_key` | `core` (exported as `OPENAI_API_KEY` for the voice provider) |
| Body / core address | `body_address` | `head` (`ws://<address>:7331/bus`), and a body's own address for `core` |
| Robot name | `name` | Both, as the module id on the bus |
| Role | `role` | Both |

Rules the screen must follow:

- Never write a key anywhere else — not to logs, not to `robot.yaml`, not to
  a Kivy `.ini`. `asimoov.android.settings.save()` is the only writer and it
  chmods the file to `0600`.
- Never ship a default key. An empty `api_key` in role `core` is an
  incomplete configuration and `main.py` refuses to start.
- Show the key masked, with a reveal toggle; never put it in a widget that
  Android can screenshot into the recents view.
- `buildozer.spec` keeps `yaml` in `source.include_exts` because personas and
  `robot.yaml` are YAML, and excludes `secrets.yaml` by pattern so a stray
  one can never be packaged. `build.sh` checks again before calling buildozer.

## Patches

Neon needed five manual steps between builds, in scripts full of
`/home/flore` and `/tmp`. They are now `patches/aiortc_*.py`, pure
`apply(source) -> source` functions driven by `hooks/p4a_hook.py`, which
python-for-android runs itself:

| Patch | Why |
| --- | --- |
| `aiortc_01_dtls_method` | the pyOpenSSL p4a builds has no `SSL.DTLS_METHOD` |
| `aiortc_02_dtls_timeout` | no `DTLSv1_get_timeout` / `DTLSv1_handle_timeout`; without a retransmission timer the handshake never completes on lossy Wi-Fi |
| `aiortc_03_peer_cert` | `get_peer_certificate(as_cryptography=True)` is not accepted; convert through PEM so the fingerprint check still runs |
| `aiortc_04_srtp_profile` | no `get_selected_srtp_profile`; call it through ctypes |

The hook also does what `build_srtp.sh` did — cross-compile libsrtp 2.5.0 and
link `pylibsrtp`'s `_binding.abi3.so` against the OpenSSL p4a built for the
arch — and what `fix_crypto.sh` did, generalised: any module installed in
`python-installs` without a matching `.pyc` in the python bundle gets
compiled and copied.

Where Neon guessed, this raises. `patch_srtp.py` fell back to assuming
`SRTP_AES128_CM_SHA1_80` when ctypes failed, which produces audio nobody can
decrypt; `aiortc_04` raises instead.

`hooks/check_patches.py` applies all four to a fixture and checks they land,
are idempotent, and fail loudly on a source that no longer matches. `build.sh`
and `android.yml` run it before the APK build, so an aiortc update breaks the
build instead of the robot.

## Foreground service

Off by default. `asimoov.android.foreground` takes a partial wake lock so the
audio loop keeps running with the screen off. To survive Android killing the
process, add a service to `buildozer.spec`:

```ini
services = core:service.py:foreground
```

and call `foreground.start_service("core")`. Without that line
`start_service` raises with the reason; it never pretends to have started
something.

## Known gaps

- The settings screen is specified here but not implemented; `main.py` reads
  the file and refuses to start without it.
- The `head` role needs WS2's PCM16 capture and playback over the bus; today
  it only renders the face from `face.state`.
- The Docker image and the APK build have not been run end to end yet (no
  Docker on the machine this was written on). The hook is written against
  python-for-android `v2024.01.21`'s real API; the first real build is the
  test.
