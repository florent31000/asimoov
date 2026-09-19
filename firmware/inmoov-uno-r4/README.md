# firmware/inmoov-uno-r4 (WS5)

`asimoov_inmoov.ino`, `protocol.md`, `config.h`. Line-based text protocol over
USB serial and `WiFiServer(5005)`, non-blocking multi-servo interpolation,
hard-coded clamps. Inherits the safety principles of
`InMoov/finger-starter/DoigtSerie/DoigtSerie.ino` (never `attach()` on boot,
`writeMicroseconds` before `attach`, `constrain` everywhere).

Owned by WS5. See `plan.md` section 4.9.
