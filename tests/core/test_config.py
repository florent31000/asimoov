"""robot.yaml loading, validation errors, and secret lookup."""

from __future__ import annotations

import pytest
from tests.core.conftest import AVATAR_ROBOT

from asimoov.core.config import ConfigError, Secrets, load_robot_config
from asimoov.core.config import asimoov_home as resolve_home

ROBOT_YAML = """apiVersion: asimoov/v1
kind: Robot
persona: ./persona.yaml
body: {type: fake}
faces: []
hub: {host: 127.0.0.1, port: 7400}
"""

PERSONA_YAML = """apiVersion: asimoov/v1
kind: Persona
name: "Test"
language: en
identity: |
  You are {name}. {body_description}
"""


def write_robot(directory, robot=ROBOT_YAML, persona=PERSONA_YAML):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "robot.yaml").write_text(robot, encoding="utf-8")
    (directory / "persona.yaml").write_text(persona, encoding="utf-8")
    return directory


def test_loads_the_avatar_robot():
    config = load_robot_config(AVATAR_ROBOT)
    assert config.body.type == "avatar"
    assert config.persona.name == "Aria"
    assert config.faces == ("web",)
    assert config.hub.port == 7331
    assert config.behaviors_dir == (AVATAR_ROBOT / "behaviors").resolve()


def test_accepts_a_direct_file_path(tmp_path):
    directory = write_robot(tmp_path / "bot")
    config = load_robot_config(directory / "robot.yaml")
    assert config.hub.port == 7400


def test_missing_file_names_the_path(tmp_path):
    with pytest.raises(ConfigError, match="no such robot configuration file"):
        load_robot_config(tmp_path / "nowhere")


def test_wrong_api_version_is_rejected(tmp_path):
    directory = write_robot(tmp_path / "bot", robot=ROBOT_YAML.replace("asimoov/v1", "asimoov/v2"))
    with pytest.raises(ConfigError, match="apiVersion"):
        load_robot_config(directory)


def test_missing_body_type_is_rejected(tmp_path):
    directory = write_robot(tmp_path / "bot", robot=ROBOT_YAML.replace("body: {type: fake}", "body: {}"))
    with pytest.raises(ConfigError, match="body.type"):
        load_robot_config(directory)


def test_broken_persona_is_reported_with_its_path(tmp_path):
    directory = write_robot(tmp_path / "bot", persona="apiVersion: asimoov/v1\nkind: Persona\nname: x\n")
    with pytest.raises(ConfigError, match="persona"):
        load_robot_config(directory)


def test_unknown_app_permission_is_rejected(tmp_path):
    robot = ROBOT_YAML + "apps: [demo]\napps_permissions: {demo: [camera.frames, teleport]}\n"
    directory = write_robot(tmp_path / "bot", robot=robot)
    with pytest.raises(ConfigError, match="teleport"):
        load_robot_config(directory)


def test_known_app_permissions_are_kept(tmp_path):
    robot = ROBOT_YAML + "apps: [demo]\napps_permissions: {demo: [camera.frames, memory.read]}\n"
    directory = write_robot(tmp_path / "bot", robot=robot)
    config = load_robot_config(directory)
    assert config.apps_permissions == {"demo": ("camera.frames", "memory.read")}


def test_bad_hub_port_is_rejected(tmp_path):
    directory = write_robot(tmp_path / "bot", robot=ROBOT_YAML.replace("port: 7400", "port: 99999"))
    with pytest.raises(ConfigError, match="hub.port"):
        load_robot_config(directory)


def test_missing_behaviors_dir_is_rejected(tmp_path):
    directory = write_robot(tmp_path / "bot", robot=ROBOT_YAML + "behaviors_dir: ./nope\n")
    with pytest.raises(ConfigError, match="behaviors_dir"):
        load_robot_config(directory)


def test_secret_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ASIMOOV_OPENAI_API_KEY", "from-env")
    secrets = Secrets(tmp_path / "secrets.yaml")
    assert secrets.get("openai_api_key") == "from-env"


def test_secret_from_file(monkeypatch, tmp_path):
    monkeypatch.delenv("ASIMOOV_OPENAI_API_KEY", raising=False)
    path = tmp_path / "secrets.yaml"
    path.write_text("openai_api_key: from-file\n", encoding="utf-8")
    assert Secrets(path).get("openai_api_key") == "from-file"


def test_missing_secret_error_names_the_source_not_the_value(monkeypatch, tmp_path):
    monkeypatch.delenv("ASIMOOV_OPENAI_API_KEY", raising=False)
    secrets = Secrets(tmp_path / "secrets.yaml")
    with pytest.raises(ConfigError) as error:
        secrets.require("openai_api_key")
    assert "ASIMOOV_OPENAI_API_KEY" in str(error.value)
    assert "secrets.yaml" in str(error.value)


def test_asimoov_home_follows_the_environment(asimoov_home):
    assert resolve_home() == asimoov_home


def test_the_openai_key_falls_back_to_the_vendor_variable(tmp_path, monkeypatch):
    """Major 13: `doctor` and the provider must read the same place."""
    secrets = Secrets(tmp_path / "secrets.yaml")
    monkeypatch.delenv("ASIMOOV_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "from-vendor-var")
    assert secrets.get("openai_api_key") == "from-vendor-var"


def test_the_asimoov_variable_wins_over_the_vendor_one(tmp_path, monkeypatch):
    secrets = Secrets(tmp_path / "secrets.yaml")
    monkeypatch.setenv("ASIMOOV_OPENAI_API_KEY", "ours")
    monkeypatch.setenv("OPENAI_API_KEY", "theirs")
    assert secrets.get("openai_api_key") == "ours"


def test_the_secrets_file_wins_over_the_vendor_variable(tmp_path, monkeypatch):
    path = tmp_path / "secrets.yaml"
    path.write_text("openai_api_key: from-file\n", encoding="utf-8")
    monkeypatch.delenv("ASIMOOV_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "theirs")
    assert Secrets(path).get("openai_api_key") == "from-file"


def test_a_missing_key_names_every_place_it_could_come_from(tmp_path, monkeypatch):
    monkeypatch.delenv("ASIMOOV_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ConfigError) as error:
        Secrets(tmp_path / "secrets.yaml").require("openai_api_key")
    message = str(error.value)
    assert "ASIMOOV_OPENAI_API_KEY" in message
    assert "OPENAI_API_KEY" in message
    assert "from-vendor-var" not in message
