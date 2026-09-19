---
title: Getting started
description: Install ASIMOOV, run the browser avatar, and say hello.
---

:::caution[Pre-alpha]
ASIMOOV is not released yet. The commands below describe the intended v1
surface; the package is not on PyPI while the workstreams land.
:::

You need Python 3.10 or newer, a microphone, and an API key from your voice
provider.

```bash
pip install asimoov
export OPENAI_API_KEY=...
asimoov run robots/avatar
```

Open `http://localhost:7331/face` and say hello. Tell it your name. Say
goodbye. Come back tomorrow and see what happens.

## Other bodies

```bash
asimoov run robots/go2       # the quadruped
asimoov run robots/inmoov    # the printed bust
asimoov doctor               # check what your machine can do
asimoov enroll --name Sam    # teach it a face from the command line
```

## Extras

The core install stays small on purpose. Each body or capability adds its own
extra:

| Extra | What it adds |
| --- | --- |
| `asimoov[go2]` | Unitree Go2 over WebRTC |
| `asimoov[inmoov]` | InMoov over serial or TCP |
| `asimoov[vision]` | Face detection and recognition |
| `asimoov[vad]` | Voice activity detection |
| `asimoov[kivy]` | The Kivy face renderer, used by the Android app |

## Cost

Speech costs money. See [the numbers](/docs/costs).
