"""Behavior loading and resolution: direct, fallback, hidden, cycles."""

from __future__ import annotations

import pytest

from asimoov.contracts.behaviors import BehaviorManifest
from asimoov.contracts.body import BodyManifest
from asimoov.core.behaviors.resolver import (
    BehaviorLoadError,
    BehaviorResolver,
    load_behavior_dir,
    load_behaviors,
    load_builtin_behaviors,
)

BUILTIN_NAMES = {
    "greet",
    "wave_hello",
    "shake_hand",
    "nod",
    "sit",
    "lie_down",
    "stand",
    "look_at",
    "express",
    "move",
    "turn",
    "stop",
}


def manifest(name, requires=(), fallback=None, duration_class="short", llm_visible=True):
    return BehaviorManifest(
        name=name,
        version=1,
        description=name,
        duration_class=duration_class,
        requires=tuple(requires),
        fallback=fallback,
        llm_visible=llm_visible,
    )


def test_builtin_behaviors_all_load():
    manifests = load_builtin_behaviors()
    assert set(manifests) == BUILTIN_NAMES
    assert manifests["shake_hand"].requires == ("gesture.hand_right",)
    assert manifests["shake_hand"].duration_class == "long"
    assert manifests["stop"].interruptible is False


def test_a_robot_directory_overrides_a_builtin(tmp_path):
    (tmp_path / "greet.yaml").write_text(
        "apiVersion: asimoov/v1\nkind: Behavior\nname: greet\nversion: 2\n"
        'description: "Custom greet"\nduration_class: instant\nrequires: []\n',
        encoding="utf-8",
    )
    manifests = load_behaviors(tmp_path)
    assert manifests["greet"].version == 2
    assert len(manifests) == len(BUILTIN_NAMES)


def test_an_unknown_capability_is_rejected(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "apiVersion: asimoov/v1\nkind: Behavior\nname: bad\nversion: 1\n"
        'description: "x"\nduration_class: short\nrequires: [gesture.teleport]\n',
        encoding="utf-8",
    )
    with pytest.raises(BehaviorLoadError, match="gesture.teleport"):
        load_behavior_dir(tmp_path)


def test_a_wrong_kind_is_rejected(tmp_path):
    (tmp_path / "bad.yaml").write_text("apiVersion: asimoov/v1\nkind: Robot\n", encoding="utf-8")
    with pytest.raises(BehaviorLoadError, match="kind"):
        load_behavior_dir(tmp_path)


def test_direct_resolution(resolver):
    resolution = resolver.resolve("wave_hello")
    assert (resolution.mode, resolution.manifest.name) == ("direct", "wave_hello")


def test_fallback_resolution(resolver):
    # FakeBody has no gesture.hand_right, so shake_hand falls back to wave_hello.
    resolution = resolver.resolve("shake_hand")
    assert (resolution.mode, resolution.manifest.name) == ("fallback", "wave_hello")
    assert resolution.runnable is True


def test_hidden_when_nothing_in_the_chain_is_satisfiable(resolver):
    resolution = resolver.resolve("nod")
    assert resolution.mode == "hidden"
    assert "gesture.nod" in resolution.reason


def test_unknown_behavior_is_hidden(resolver):
    assert resolver.resolve("teleport").mode == "hidden"


def test_say_fallback_is_runnable():
    manifests = {"hug": manifest("hug", requires=("gesture.hand_left",), fallback="say")}
    resolver = BehaviorResolver(manifests, ("gesture.wave",))
    resolution = resolver.resolve("hug")
    assert (resolution.mode, resolution.runnable) == ("say", True)


def test_a_fallback_cycle_is_detected():
    manifests = {
        "a": manifest("a", requires=("gesture.dance",), fallback="b"),
        "b": manifest("b", requires=("gesture.heart",), fallback="a"),
    }
    resolver = BehaviorResolver(manifests, ())
    assert "cycle" in resolver.resolve("a").reason


def test_an_unknown_fallback_is_reported():
    manifests = {"a": manifest("a", requires=("gesture.dance",), fallback="ghost")}
    assert "ghost" in BehaviorResolver(manifests, ()).resolve("a").reason


def test_visible_hides_impossible_and_non_llm_behaviors(resolver):
    names = {manifest.name for manifest in resolver.visible()}
    assert "wave_hello" in names
    assert "shake_hand" in names  # via its fallback
    assert "nod" not in names
    assert "move" not in names  # FakeBody cannot walk


def test_visible_respects_llm_visible():
    manifests = {"secret": manifest("secret", llm_visible=False)}
    assert BehaviorResolver(manifests, ()).visible() == ()


def test_primitive_comes_from_the_body_manifest():
    body = BodyManifest(
        name="b",
        kind_of_body="virtual",
        capabilities=("gesture.wave",),
        implements={"wave_hello": {"primitive": "gesture", "arg": "wave"}},
    )
    resolver = BehaviorResolver.from_body(load_builtin_behaviors(), body)
    assert resolver.primitive("wave_hello")["arg"] == "wave"
    assert resolver.primitive("sit") is None
