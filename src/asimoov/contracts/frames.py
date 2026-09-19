"""The binary `frame.*` codec (frames v1), frozen with contracts v1.2.

Camera frames never cross the bus as JSON and the core never subscribes to
them: the hub relays the bytes below between clients untouched (plan.md
section 4.3). Every producer and consumer -- the face page, the Go2's front
camera, perception's `ws_frames` camera -- uses this one codec, so any
producer can feed any consumer.

Layout, big-endian:

===== ====== =========================================================
bytes field  meaning
===== ====== =========================================================
0     ver    always 1 (`FRAME_VERSION`)
1     tlen   length in bytes of the UTF-8 topic that follows the header
2-3   seq    wrapping frame counter, for drop detection
4-7   ts_ms  capture time, Unix milliseconds modulo 2**32
===== ====== =========================================================

then ``tlen`` bytes of topic (``frame.browser``, ``frame.go2``, ...) and
the JPEG payload to the end of the message. The topic is spelled out rather
than numbered so a new camera needs no registry entry on both sides.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

FRAME_HEADER = struct.Struct("!BBHI")
FRAME_HEADER_SIZE = FRAME_HEADER.size
FRAME_VERSION = 1
_JPEG_SOI = b"\xff\xd8\xff"

TOPIC_FRAME_GO2 = "frame.go2"


@dataclass(frozen=True)
class FrameMessage:
    """One decoded binary frame message."""

    topic: str
    seq: int
    ts_ms: int
    jpeg: bytes


def encode_frame(topic: str, jpeg: bytes, *, seq: int = 0, ts_ms: int = 0) -> bytes:
    """Encode a frame message.

    Raises:
        ValueError: if the UTF-8 topic is empty or longer than 255 bytes.
    """
    topic_bytes = topic.encode("utf-8")
    if not 1 <= len(topic_bytes) <= 255:
        raise ValueError(f"topic must be 1..255 bytes, got {len(topic_bytes)}")
    header = FRAME_HEADER.pack(FRAME_VERSION, len(topic_bytes), seq & 0xFFFF, ts_ms & 0xFFFFFFFF)
    return header + topic_bytes + jpeg


def decode_frame(message: bytes, *, default_topic: str | None = None) -> FrameMessage:
    """Decode a binary frame message.

    A message that is a bare JPEG (no header) is accepted when
    `default_topic` is given, so a producer that streams raw JPEG still
    works; anything else is an error rather than a silently dropped frame.

    Raises:
        ValueError: on a truncated message, an unknown header version, or a
            bare JPEG with no `default_topic`.
    """
    if message[:3] == _JPEG_SOI:
        if default_topic is None:
            raise ValueError("headerless JPEG frame and no default_topic to attribute it to")
        return FrameMessage(topic=default_topic, seq=0, ts_ms=0, jpeg=bytes(message))
    if len(message) < FRAME_HEADER_SIZE:
        raise ValueError(f"frame message too short: {len(message)} bytes")
    version, topic_len, seq, ts_ms = FRAME_HEADER.unpack_from(message, 0)
    if version != FRAME_VERSION:
        raise ValueError(f"unsupported frame header version {version}")
    end = FRAME_HEADER_SIZE + topic_len
    if len(message) <= end:
        raise ValueError("frame message has a topic but no payload")
    return FrameMessage(
        topic=message[FRAME_HEADER_SIZE:end].decode("utf-8"),
        seq=seq,
        ts_ms=ts_ms,
        jpeg=bytes(message[end:]),
    )
