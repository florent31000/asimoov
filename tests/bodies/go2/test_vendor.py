"""The vendored driver: patches present, constants in sync, license kept.

These checks parse the vendored sources instead of importing them: importing
`unitree_webrtc_connect` pulls in `aiortc`, which CI does not install (only
the ``[dev]`` extra).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from asimoov.bodies.go2 import sport as sport_mod

VENDOR = Path(sport_mod.__file__).parent / "vendor"
PACKAGE = VENDOR / "unitree_webrtc_connect"


def module_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def literal_dict(path: Path, name: str) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path}")


def test_every_vendored_file_parses() -> None:
    files = sorted(PACKAGE.rglob("*.py"))
    assert files
    for path in files:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_license_is_kept() -> None:
    license_text = (PACKAGE / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in license_text
    assert "Copyright" in license_text
    assert "unitree_webrtc_connect" in (VENDOR / "README.md").read_text(encoding="utf-8")


def test_timeout_cleanup_patch_is_present() -> None:
    assert "cancel_resolve" in module_names(PACKAGE / "msgs" / "future_resolver.py")
    assert "cancel_request" in module_names(PACKAGE / "msgs" / "pub_sub.py")


def test_publish_uses_the_running_loop() -> None:
    source = (PACKAGE / "msgs" / "pub_sub.py").read_text(encoding="utf-8")
    assert "asyncio.get_event_loop()" not in source


def test_blocking_calls_run_off_the_event_loop() -> None:
    source = (PACKAGE / "webrtc_driver.py").read_text(encoding="utf-8")
    for blocking in ("discover_ip_sn", "send_sdp_to_local_peer", "send_sdp_to_remote_peer"):
        assert f"asyncio.to_thread(\n            {blocking}" in source or (
            f"asyncio.to_thread({blocking}" in source
        ), f"{blocking} must not block the event loop"


def test_msgs_is_a_real_package() -> None:
    assert (PACKAGE / "msgs" / "__init__.py").exists()


@pytest.mark.parametrize(
    ("constant", "vendored"),
    [
        (sport_mod.TOPIC_SPORT, "SPORT_MOD"),
        (sport_mod.TOPIC_MOTION_SWITCHER, "MOTION_SWITCHER"),
        (sport_mod.TOPIC_OBSTACLES_AVOID, "OBSTACLES_AVOID"),
        (sport_mod.TOPIC_WIRELESS_CONTROLLER, "WIRELESS_CONTROLLER"),
        (sport_mod.TOPIC_SPORT_MODE_STATE, "LF_SPORT_MOD_STATE"),
        (sport_mod.TOPIC_LOW_STATE, "LOW_STATE"),
    ],
)
def test_topics_match_the_vendored_constants(constant: str, vendored: str) -> None:
    assert constant == literal_dict(PACKAGE / "constants.py", "RTC_TOPIC")[vendored]


def test_sport_commands_match_the_vendored_constants() -> None:
    vendored = literal_dict(PACKAGE / "constants.py", "SPORT_CMD")
    for name, api_id in sport_mod.SPORT_CMD.items():
        assert vendored[name] == api_id, f"{name} drifted from the vendored api id"


def test_action_map_only_names_known_commands() -> None:
    for action, command in sport_mod.ACTION_MAP.items():
        assert command in sport_mod.SPORT_CMD, f"{action} maps to unknown {command}"
