"""Linear PCM16 resampling, used only when a device cannot run at 24 kHz."""

from __future__ import annotations

import numpy as np


def resample_pcm16(pcm16: bytes, src_hz: int, dst_hz: int) -> bytes:
    """Resample PCM16 mono ``pcm16`` from ``src_hz`` to ``dst_hz``.

    Linear interpolation (numpy only, no scipy): enough for speech at these
    rates, and what Neon shipped for its 16 kHz -> 24 kHz path.

    Raises:
        ValueError: if either rate is not strictly positive.
    """
    if src_hz <= 0 or dst_hz <= 0:
        raise ValueError(f"invalid sample rates: {src_hz} -> {dst_hz}")
    if src_hz == dst_hz or len(pcm16) < 4:
        return pcm16
    samples = np.frombuffer(pcm16, dtype=np.int16)
    n_in = samples.size
    n_out = max(1, int(round(n_in * dst_hz / src_hz)))
    x_in = np.arange(n_in, dtype=np.float32)
    x_out = np.linspace(0, n_in - 1, n_out, dtype=np.float32)
    resampled = np.interp(x_out, x_in, samples.astype(np.float32))
    return np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()
