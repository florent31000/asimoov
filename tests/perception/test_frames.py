"""The binary `frame.*` codec."""

from __future__ import annotations

import pytest

from asimoov.perception.frames import FRAME_HEADER_SIZE, decode_frame, encode_frame

JPEG = b"\xff\xd8\xff\xe0" + b"body" * 8 + b"\xff\xd9"


def test_round_trip():
    message = encode_frame("frame.browser", JPEG, seq=7, ts_ms=1234)
    decoded = decode_frame(message)
    assert decoded.topic == "frame.browser"
    assert decoded.seq == 7
    assert decoded.ts_ms == 1234
    assert decoded.jpeg == JPEG


def test_header_is_eight_bytes():
    assert FRAME_HEADER_SIZE == 8
    message = encode_frame("frame.go2", JPEG)
    assert len(message) == 8 + len("frame.go2") + len(JPEG)


def test_sequence_and_timestamp_wrap_instead_of_overflowing():
    decoded = decode_frame(encode_frame("frame.go2", JPEG, seq=70000, ts_ms=2**33 + 5))
    assert decoded.seq == 70000 & 0xFFFF
    assert decoded.ts_ms == 5


def test_a_bare_jpeg_needs_an_explicit_topic():
    with pytest.raises(ValueError, match="default_topic"):
        decode_frame(JPEG)
    assert decode_frame(JPEG, default_topic="frame.browser").topic == "frame.browser"


def test_a_truncated_message_is_an_error():
    with pytest.raises(ValueError, match="too short"):
        decode_frame(b"\x01\x02\x03")


def test_an_unknown_header_version_is_an_error():
    message = bytearray(encode_frame("frame.go2", JPEG))
    message[0] = 9
    with pytest.raises(ValueError, match="version"):
        decode_frame(bytes(message))


def test_a_frame_without_payload_is_an_error():
    with pytest.raises(ValueError, match="no payload"):
        decode_frame(encode_frame("frame.go2", b""))


def test_an_empty_topic_cannot_be_encoded():
    with pytest.raises(ValueError, match="1..255"):
        encode_frame("", JPEG)
