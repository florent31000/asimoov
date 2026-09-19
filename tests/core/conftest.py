"""Shared fixtures for the core tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from asimoov.contracts.fakes import FakeBody
from asimoov.core.behaviors.resolver import BehaviorResolver, load_behaviors
from asimoov.core.config import load_robot_config

REPO_ROOT = Path(__file__).resolve().parents[2]
AVATAR_ROBOT = REPO_ROOT / "robots" / "avatar"
REPLAY_DIR = REPO_ROOT / "tests" / "fixtures" / "replays"


@pytest.fixture(autouse=True)
def asimoov_home(tmp_path, monkeypatch):
    """Keep every test out of the real ~/.asimoov."""
    home = tmp_path / "asimoov-home"
    home.mkdir()
    monkeypatch.setenv("ASIMOOV_HOME", str(home))
    return home


@pytest.fixture
def avatar_config():
    return load_robot_config(AVATAR_ROBOT)


@pytest.fixture
def fake_body():
    return FakeBody()


@pytest.fixture
def resolver(fake_body):
    return BehaviorResolver.from_body(load_behaviors(), fake_body.manifest)
