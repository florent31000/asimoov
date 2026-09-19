# firmware/inmoov-uno-r4

Firmware for the InMoov bust on an Arduino UNO R4 WiFi. Line protocol over
USB serial and TCP, non-blocking multi-servo interpolation, hard limits in
`config.h`. Driven by `src/asimoov/bodies/inmoov/`.

```
asimoov_inmoov/
  asimoov_inmoov.ino   the sketch
  config.h             channel table (names, limits, rest, pulse widths)
  secrets.h.example    Wi-Fi credentials template (copy to secrets.h)
protocol.md            the command set
ci-compile.md          the arduino-cli job for CI (WS7)
```

The sketch lives in its own `asimoov_inmoov/` folder because Arduino requires
a sketch folder to carry the name of its main `.ino`.

## Dependencies

- Core `arduino:renesas_uno` (UNO R4 boards).
- `Servo`, `Wire`, `WiFiS3`, `Arduino_LED_Matrix`: bundled with the IDE / core.
- `Adafruit PWM Servo Driver Library` (pulls `Adafruit BusIO`): install from
  the library manager, or `arduino-cli lib install "Adafruit PWM Servo Driver Library"`.

## Build and flash

```sh
arduino-cli compile --fqbn arduino:renesas_uno:unor4wifi firmware/inmoov-uno-r4/asimoov_inmoov
arduino-cli upload  --fqbn arduino:renesas_uno:unor4wifi -p COM5 firmware/inmoov-uno-r4/asimoov_inmoov
```

In the IDE: open `asimoov_inmoov/asimoov_inmoov.ino`, board "Arduino UNO R4
WiFi", then upload.

## Wi-Fi

Serial only by default. For the TCP link, copy `secrets.h.example` to
`secrets.h`, fill in the network, and set `ASIMOOV_WIFI_ENABLED` to `1` in
`config.h`. `secrets.h` is git-ignored and never committed.

## Safety

Nothing is attached at boot and nothing moves until an explicit `E`. The
limits in `config.h` are hard: the `L` command can only tighten them. See
`docs/bodies/inmoov.md` for the wiring, the power rail and the bench
procedure.
