"""Parsing the `perception:` section of `robot.yaml`."""

from __future__ import annotations

import pytest
import yaml

from asimoov.perception import PerceptionError
from asimoov.perception.config import load_config, parse_config

ROBOT_YAML = """
apiVersion: asimoov/v1
kind: Robot
persona: ./persona.yaml
body: {type: avatar}
perception: {face_id: {enabled: true, camera: ws_frames, fov_h_deg: 90}}
hub: {host: 0.0.0.0, port: 7331}
"""


def test_defaults_without_a_perception_section():
    config = parse_config({"kind": "Robot"})
    assert config.face_id.enabled is False
    assert config.face_id.camera == "ws_frames"
    assert config.vad_enabled is False
    assert config.hub_url == "ws://127.0.0.1:7331/bus"


def test_full_section():
    config = parse_config(yaml.safe_load(ROBOT_YAML))
    assert config.face_id.enabled is True
    assert config.face_id.camera == "ws_frames"
    assert config.face_id.fov_h_deg == 90.0


def test_wildcard_bind_address_becomes_loopback():
    assert parse_config(yaml.safe_load(ROBOT_YAML)).hub_url == "ws://127.0.0.1:7331/bus"


def test_a_remote_hub_is_kept_as_is():
    config = parse_config({"hub": {"host": "192.168.1.10", "port": 7400}})
    assert config.hub_url == "ws://192.168.1.10:7400/bus"


def test_an_unknown_key_is_refused_instead_of_ignored():
    with pytest.raises(PerceptionError, match="unknown key"):
        parse_config({"perception": {"face_id": {"enabled": True, "fps": 30}}})
    with pytest.raises(PerceptionError, match="unknown key"):
        parse_config({"perception": {"faceid": {}}})


def test_a_malformed_section_is_refused():
    with pytest.raises(PerceptionError, match="must be a mapping"):
        parse_config({"perception": ["face_id"]})


def test_loading_a_file(tmp_path):
    path = tmp_path / "robot.yaml"
    path.write_text(ROBOT_YAML, encoding="utf-8")
    assert load_config(path).face_id.enabled is True


def test_a_missing_file_is_named_in_the_error(tmp_path):
    with pytest.raises(PerceptionError, match="config not found"):
        load_config(tmp_path / "nope.yaml")


def test_the_shipped_avatar_robot_parses():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    config = load_config(root / "robots" / "avatar" / "robot.yaml")
    assert config.face_id.camera == "ws_frames"
    assert config.hub_url == "ws://127.0.0.1:7331/bus"
