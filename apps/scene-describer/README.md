# apps/scene-describer

Example app: describes what the camera sees, sending a single image to a
vision LLM on explicit request only. Reference implementation of the
`app.v1` manifest (permissions, degraded mode without `camera.front`).

Not owned by WS0. See `plan.md` section 4.3 for the full manifest example
this app implements, and `asimoov/contracts/examples/app.scene-describer.yaml`
(owned by WS0) for the frozen example manifest used in contract tests.
