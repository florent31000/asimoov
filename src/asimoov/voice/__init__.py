"""Voice: OpenAI Realtime provider, session lifecycle, barge-in, audio I/O.

Device-specific modules (`audio.capture_desktop`, `audio.playback_desktop`,
`audio.capture_android`, `audio.playback_android`) are imported explicitly by
whoever opens a device, so importing this package never touches `sounddevice`
or `jnius`.
"""

from asimoov.voice.audio.resample import resample_pcm16
from asimoov.voice.audio.tracker import (
    DEFAULT_TAIL_MS,
    HeadTracker,
    MicGate,
    StreamPlaybackTracker,
)
from asimoov.voice.barge_in import (
    BargeInConfig,
    BargeInController,
    BargeInDetector,
    RelativeEnergyVad,
)
from asimoov.voice.openai_realtime import OpenAIRealtimeProvider
from asimoov.voice.session import SessionLimits, SessionManager

__all__ = [
    "DEFAULT_TAIL_MS",
    "BargeInConfig",
    "BargeInController",
    "BargeInDetector",
    "HeadTracker",
    "MicGate",
    "OpenAIRealtimeProvider",
    "RelativeEnergyVad",
    "SessionLimits",
    "SessionManager",
    "StreamPlaybackTracker",
    "resample_pcm16",
]
