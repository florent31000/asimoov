"""Paths and the fake hardware the end-to-end tests assemble a robot around."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REPLAY_DIR = REPO_ROOT / "tests" / "fixtures" / "replays"
ROBOTS = REPO_ROOT / "robots"

# pytest's importlib import mode does not put a test directory on `sys.path`:
# make the doubles the body workstreams already wrote importable rather than
# copying them here.
for _helpers in (
    REPO_ROOT / "tests" / "bodies" / "go2",
    REPO_ROOT / "tests" / "bodies" / "inmoov",
):
    if str(_helpers) not in sys.path:
        sys.path.append(str(_helpers))
