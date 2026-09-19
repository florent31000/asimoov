"""Resolving bodies, faces, voice providers and perception modules by name.

Everything is an entry point (`pyproject.toml` declares the four groups), so
a body shipped by someone else plugs in without touching the core. The
``fake`` implementations of the groups that have no entry point yet come
from the fallback registry below, which is what makes
``--voice fake --body fake`` work on a bare install.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from importlib.metadata import entry_points

log = logging.getLogger(__name__)

BODY_GROUP = "asimoov.bodies"
FACE_GROUP = "asimoov.faces"
VOICE_GROUP = "asimoov.voice_providers"
PERCEPTION_GROUP = "asimoov.perception"


class PluginError(ValueError):
    """Raised when a named plugin does not exist; the message lists what does."""


def _fake_body():
    from asimoov.contracts.fakes import FakeBody

    return FakeBody


def _fake_voice():
    from asimoov.contracts.fakes import FakeVoiceProvider

    return FakeVoiceProvider


def _fake_face():
    from asimoov.contracts.fakes import FakeFaceRenderer

    return FakeFaceRenderer


FALLBACKS: dict[str, dict[str, Callable[[], type]]] = {
    BODY_GROUP: {"fake": _fake_body},
    VOICE_GROUP: {"fake": _fake_voice},
    FACE_GROUP: {"fake": _fake_face},
}


def available(group: str) -> tuple[str, ...]:
    """Every plugin name in ``group``, entry points plus fallbacks."""
    names = {entry.name for entry in entry_points(group=group)}
    names.update(FALLBACKS.get(group, {}))
    return tuple(sorted(names))


def load_plugin(group: str, name: str) -> type:
    """Load the class registered as ``name`` in ``group``.

    Raises:
        PluginError: if no plugin goes by that name, listing the names that
            do exist so the message is actionable.
    """
    for entry in entry_points(group=group):
        if entry.name == name:
            return entry.load()
    fallback = FALLBACKS.get(group, {}).get(name)
    if fallback is not None:
        return fallback()
    raise PluginError(
        f"unknown {group.split('.')[-1]} {name!r}; available: {', '.join(available(group)) or 'none'}"
    )
