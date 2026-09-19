# android (WS7)

Android packaging: `Dockerfile` (Ubuntu 22.04, buildozer, pinned p4a, NDK 25b,
SDK 35), `build.sh`, `patches/` (Neon patches applied automatically by a p4a
hook), `main.py`, `README.md`. CI: `.github/workflows/android.yml`.

Owned by WS7. Forbidden core deps (pydantic, onnxruntime, opencv, scipy,
aiohttp, msgspec, orjson, uvloop, torch) must never leak into the `[core]`
install used by the APK. See `plan.md` section 4.10.
