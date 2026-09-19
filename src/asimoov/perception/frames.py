"""Re-export of the frozen binary `frame.*` codec (`contracts.frames`).

The codec started here and was promoted to `contracts/frames.py` in v1.2,
once the face page and the Go2 converged on it. This module stays so
perception's imports read locally; there is no second implementation.
"""

from __future__ import annotations

from asimoov.contracts.frames import (
    FRAME_HEADER,
    FRAME_HEADER_SIZE,
    FRAME_VERSION,
    FrameMessage,
    decode_frame,
    encode_frame,
)

__all__ = [
    "FRAME_HEADER",
    "FRAME_HEADER_SIZE",
    "FRAME_VERSION",
    "FrameMessage",
    "decode_frame",
    "encode_frame",
]
