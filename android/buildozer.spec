[app]

title = ASIMOOV
package.name = app
package.domain = io.asimoov
version = 0.1.0

# build.sh stages `src/asimoov` and `robots/avatar` into this directory before
# buildozer runs; both are gitignored. Nothing here is a secret: the API key,
# the body address and the robot name are written at runtime to
# getFilesDir()/secrets.yaml by the settings screen.
source.dir = .
source.include_exts = py,kv,json,yaml,txt,png,ttf
source.exclude_exts = spec,apk,aab
source.exclude_dirs = bin, patches, hooks, .buildozer, __pycache__, tests
source.exclude_patterns = secrets.yaml, */secrets.yaml, .asimoov/*, */.asimoov/*

# Core install (plan.md 4.10): stdlib + websockets, pyyaml, numpy, jsonschema,
# certifi. Never pydantic, onnxruntime, opencv, scipy, aiohttp, msgspec,
# orjson, uvloop or torch.
#
# The block after `pyee` is the [go2] WebRTC stack. python-for-android does
# not resolve transitive dependencies, so they are all listed explicitly.
# Drop everything from `aiortc` down for a face-only or InMoov-only APK.
requirements = python3,
    setuptools,
    kivy==2.3.0,
    pyjnius,
    android,
    websockets,
    pyyaml,
    numpy,
    jsonschema,
    certifi,
    openssl,
    pyopenssl,
    cryptography,
    aiortc,
    aioice,
    av,
    pylibsrtp,
    pyee,
    google-crc32c,
    ifaddr,
    dnspython,
    idna,
    typing_extensions,
    packaging

orientation = landscape
fullscreen = 1

android.api = 35
android.minapi = 26
android.ndk_api = 26
android.ndk = 25b
android.sdk = 35
android.archs = arm64-v8a
android.accept_sdk_license = True
android.enable_androidx = True
android.apptheme = @android:style/Theme.NoTitleBar
android.gradle_dependencies = androidx.core:core:1.13.1

# CAMERA and RECORD_AUDIO are requested at runtime by
# asimoov.android.permissions; FOREGROUND_SERVICE* keeps the core alive with
# the screen off (role `core` only).
android.permissions =
    INTERNET,
    ACCESS_NETWORK_STATE,
    ACCESS_WIFI_STATE,
    CHANGE_WIFI_STATE,
    CAMERA,
    RECORD_AUDIO,
    MODIFY_AUDIO_SETTINGS,
    WAKE_LOCK,
    FOREGROUND_SERVICE,
    FOREGROUND_SERVICE_MICROPHONE,
    POST_NOTIFICATIONS

# Pinned clone provided by the Docker image; never a moving master.
p4a.source_dir = /opt/python-for-android
p4a.bootstrap = sdl2
p4a.hook = ./hooks/p4a_hook.py

[buildozer]

log_level = 2
warn_on_root = 0
build_dir = ./.buildozer
bin_dir = ./bin
