"""App loading: folder manifests, permissions, degraded mode, plugins."""

from __future__ import annotations

import pytest

from asimoov.core.apps.loader import AppLoadError, load_apps
from asimoov.core.plugins import BODY_GROUP, VOICE_GROUP, PluginError, available, load_plugin

MANIFEST = """apiVersion: asimoov/v1
kind: App
name: scene-describer
version: 0.1.0
description: "Describe what the camera sees."
entrypoint: asimoov_app_scene_describer:App
permissions: [camera.frames, llm.vision]
provides:
  tools: [describe_scene]
  persona_fragment: "You can describe what you see."
requires: {capabilities: [camera.front], python: ">=3.10"}
degraded:
  - when_missing: camera.front
    disable: [describe_scene]
    persona_fragment: "You have no camera."
"""


def write_app(root, manifest=MANIFEST, name="scene-describer"):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "asimoov-app.yaml").write_text(manifest, encoding="utf-8")
    return root


def test_an_app_loads_with_its_permissions(tmp_path):
    apps = load_apps(
        ("scene-describer",),
        capabilities=("camera.front",),
        permissions={"scene-describer": ("camera.frames", "llm.vision")},
        search_dirs=(write_app(tmp_path),),
    )
    app = apps[0]
    assert app.loaded is True
    assert app.granted == ("camera.frames", "llm.vision")
    assert app.denied == ()
    assert app.degraded is False
    assert app.tools == ("describe_scene",)
    assert app.fragments == ("You can describe what you see.",)


def test_permissions_are_denied_by_default(tmp_path):
    app = load_apps(
        ("scene-describer",),
        capabilities=("camera.front",),
        search_dirs=(write_app(tmp_path),),
    )[0]
    assert app.loaded is True
    assert app.granted == ()
    assert app.denied == ("camera.frames", "llm.vision")
    assert app.degraded is True


def test_a_missing_capability_activates_degraded_mode(tmp_path):
    app = load_apps(
        ("scene-describer",),
        capabilities=(),
        permissions={"scene-describer": ("camera.frames", "llm.vision")},
        search_dirs=(write_app(tmp_path),),
    )[0]
    assert app.loaded is True
    assert app.disabled_tools == ("describe_scene",)
    assert app.tools == ()
    assert "You have no camera." in app.fragments


def test_a_missing_capability_with_no_rule_stops_the_app(tmp_path):
    manifest = MANIFEST.replace(
        "degraded:\n  - when_missing: camera.front\n    disable: [describe_scene]\n"
        '    persona_fragment: "You have no camera."\n',
        "degraded: []\n",
    )
    app = load_apps(
        ("scene-describer",), capabilities=(), search_dirs=(write_app(tmp_path, manifest),)
    )[0]
    assert app.loaded is False
    assert "camera.front" in app.reason


def test_an_unknown_app_is_reported(tmp_path):
    app = load_apps(("ghost",), search_dirs=(tmp_path,))[0]
    assert app.loaded is False
    assert "not found" in app.reason


def test_a_malformed_manifest_names_the_file(tmp_path):
    with pytest.raises(AppLoadError, match="kind"):
        load_apps(
            ("bad",),
            search_dirs=(write_app(tmp_path, "apiVersion: asimoov/v1\nkind: Robot\n", name="bad"),),
        )


def test_an_impossible_python_requirement_stops_the_app(tmp_path):
    manifest = MANIFEST.replace('python: ">=3.10"', 'python: ">=9.99"')
    app = load_apps(
        ("scene-describer",),
        capabilities=("camera.front",),
        search_dirs=(write_app(tmp_path, manifest),),
    )[0]
    assert app.loaded is False
    assert "9.99" in app.reason


def test_an_unparsable_python_requirement_is_an_error(tmp_path):
    manifest = MANIFEST.replace('python: ">=3.10"', 'python: "^3.10"')
    with pytest.raises(AppLoadError, match="requires.python"):
        load_apps(("scene-describer",), search_dirs=(write_app(tmp_path, manifest),))


def test_the_fake_body_comes_from_an_entry_point():
    assert "fake" in available(BODY_GROUP)
    assert load_plugin(BODY_GROUP, "fake").__name__ == "FakeBody"


def test_the_fake_voice_comes_from_the_fallback_registry():
    assert "fake" in available(VOICE_GROUP)
    assert load_plugin(VOICE_GROUP, "fake").__name__ == "FakeVoiceProvider"


def test_an_unknown_plugin_lists_what_exists():
    with pytest.raises(PluginError) as error:
        load_plugin(BODY_GROUP, "unicorn")
    assert "fake" in str(error.value)
