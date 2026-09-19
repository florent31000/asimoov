"""ASIMOOV perception process (WS3).

Runs as its own OS process (`python -m asimoov.perception`), connects to the
core hub as a bus client, and publishes `percept.*` envelopes. Camera frames
are decoded, used, and dropped: they are never written to disk and never
published by this process.
"""

from __future__ import annotations

__all__ = ["PerceptionError"]


class PerceptionError(RuntimeError):
    """A perception component cannot run (missing model, camera, or extra)."""
