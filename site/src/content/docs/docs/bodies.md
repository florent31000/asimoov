---
title: Bodies
description: Supported bodies and how to write an adapter for your own.
---

| Body | What it is | Status |
| --- | --- | --- |
| **Avatar** | Eyes in your browser. No robot, no hardware, no account. | Ready |
| **InMoov** | The open-source 3D-printed humanoid. Head, arms, hands, jaw. | Ready |
| **Unitree Go2** | The quadruped. Gestures, gaze by body rotation, front camera. | Ready |
| **Reachy Mini** | The desk robot from Pollen Robotics and Hugging Face. | In progress |

## Adding your own

A body adapter is one class implementing the `Body` ABC from
`asimoov.contracts.body`, plus a manifest that lists what the body can do:
capabilities, gestures, safety limits.

Register it through the `asimoov.bodies` entry point group, then run the
conformance suite:

```python
from asimoov.bodies.conformance import run_conformance

run_conformance(MyBody(...))
```

The suite checks the manifest against the frozen vocabularies, that every
declared gesture is callable, that long behaviors answer asynchronously, that
`stop_all` is honoured, and — if the body implements `simulate_disconnect()` —
that losing the link stops everything within a second.

See [Contributing](https://github.com/florent31000/asimoov/blob/main/CONTRIBUTING.md)
for the full checklist.
