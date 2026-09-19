"""Resampling, and the import guards that keep device backends optional."""

from __future__ import annotations

import pytest

from asimoov.voice.audio import capture_android, capture_desktop, playback_android, playback_desktop
from asimoov.voice.audio.resample import resample_pcm16

RATE = 24000


def test_resample_changes_the_frame_count_by_the_rate_ratio():
    pcm = b"\x00\x01" * 1600  # 100 ms at 16 kHz
    out = resample_pcm16(pcm, 16000, 24000)
    assert len(out) == 2400 * 2


def test_resample_is_a_no_op_at_the_same_rate():
    pcm = b"\x00\x01" * 100
    assert resample_pcm16(pcm, RATE, RATE) is pcm


def test_resample_rejects_an_invalid_rate():
    with pytest.raises(ValueError):
        resample_pcm16(b"\x00\x01" * 10, 0, RATE)


def test_device_backends_import_without_their_optional_dependency():
    for module in (capture_desktop, playback_desktop, capture_android, playback_android):
        assert isinstance(module.available(), bool)


async def test_desktop_source_reports_a_missing_sounddevice():
    if capture_desktop.available():
        pytest.skip("sounddevice is installed on this host")
    source = capture_desktop.DesktopAudioSource()
    with pytest.raises(RuntimeError, match="sounddevice"):
        await source.start(lambda chunk: None)


async def test_android_sink_reports_a_missing_jnius():
    if playback_android.available():
        pytest.skip("pyjnius is available on this host")
    sink = playback_android.AndroidAudioSink()
    with pytest.raises(RuntimeError, match="pyjnius"):
        await sink.play("item_1", b"\x00\x00")
