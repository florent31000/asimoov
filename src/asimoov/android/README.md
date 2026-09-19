# android

Android glue used by the APK: runtime permissions (`permissions.py`), the
wake lock and optional foreground service (`foreground.py`), and the
settings file written to `getFilesDir()/secrets.yaml` (`settings.py`).

All three are import-guarded: they import and run on a laptop, where
`is_android()` is False and every Android call is a logged no-op.

Packaging (Docker, buildozer, patches, the settings screen spec and the
three phone roles) lives in the top-level `android/` directory.
