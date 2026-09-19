# CI: compiling the firmware

For WS7 to include in `.github/workflows/`. The job compiles the sketch for
the UNO R4 WiFi on every change under `firmware/`; it never uploads, and it
never needs `secrets.h` (the default `config.h` ships with
`ASIMOOV_WIFI_ENABLED 0`).

Verified locally with `arduino-cli` 1.5.1, core `arduino:renesas_uno` 1.6.0,
`Adafruit PWM Servo Driver Library` 3.0.3: 72112 bytes of flash (27 %),
10808 bytes of RAM (32 %) for the one-finger bench config, and 81752 / 12028
for the full-bust table with Wi-Fi enabled. No warning with `--warnings all`.

```yaml
name: firmware

on:
  push:
    paths: ["firmware/**", ".github/workflows/firmware.yml"]
  pull_request:
    paths: ["firmware/**", ".github/workflows/firmware.yml"]

jobs:
  compile:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: arduino/setup-arduino-cli@v1
      - name: Install core and libraries
        run: |
          arduino-cli core update-index
          arduino-cli core install arduino:renesas_uno
          arduino-cli lib install "Adafruit PWM Servo Driver Library"
      - name: Compile (bench config)
        run: |
          arduino-cli compile --warnings all \
            --fqbn arduino:renesas_uno:unor4wifi \
            firmware/inmoov-uno-r4/asimoov_inmoov
```

To also cover the full-bust table and the Wi-Fi path, add a second step that
compiles a copy of the sketch with the bust channel table uncommented,
`ASIMOOV_WIFI_ENABLED 1` and a dummy `secrets.h` (never a real one):

```yaml
      - name: Compile (bust + wifi)
        run: |
          cp -r firmware/inmoov-uno-r4/asimoov_inmoov /tmp/asimoov_inmoov
          printf '#pragma once\n#define WIFI_SSID "ci"\n#define WIFI_PASS "ci"\n' > /tmp/asimoov_inmoov/secrets.h
          # uncomment the bust table in /tmp/asimoov_inmoov/config.h, then:
          arduino-cli compile --fqbn arduino:renesas_uno:unor4wifi /tmp/asimoov_inmoov
```
