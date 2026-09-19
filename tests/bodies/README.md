# tests/bodies (WS4, WS5)

`go2/` (mocked datachannel, MotionController, forbidden flips) owned by WS4.
`inmoov/` (protocol simulator, conformance) owned by WS5. Both adapters run
the shared conformance suite from `asimoov.bodies.conformance.run_conformance`
(owned by WS0, frozen). See `plan.md` section 4.5.
