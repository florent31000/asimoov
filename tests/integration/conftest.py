"""Shared setup for the end-to-end tests: real components, fake hardware.

These tests assemble a whole `Runtime` -- hub, bus, mind, body adapter, face
renderer -- and drive it with a recorded percept stream. Only the last inch
is faked: the Go2's WebRTC link and the InMoov's firmware. Nothing here
opens a camera, a microphone or a connection to a real robot.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# pytest's importlib import mode does not put a test directory on `sys.path`.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


@pytest.fixture(autouse=True)
def asimoov_home(tmp_path, monkeypatch):
    """Keep every test out of the real ~/.asimoov (hub token, memory, metrics)."""
    home = tmp_path / "asimoov-home"
    home.mkdir()
    monkeypatch.setenv("ASIMOOV_HOME", str(home))
    return home
