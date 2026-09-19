"""app.v1.json restricts `permissions` to `vocab.APP_PERMISSIONS` (contracts v1.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from asimoov.contracts.vocab import APP_PERMISSIONS, is_app_permission

REPO_ROOT = Path(__file__).parents[2]
SCHEMA_PATH = REPO_ROOT / "src" / "asimoov" / "contracts" / "schemas" / "app.v1.json"

_BASE_MANIFEST = {
    "apiVersion": "asimoov/v1",
    "kind": "App",
    "name": "test-app",
    "version": "0.1.0",
    "description": "A test app.",
    "entrypoint": "test_app:App",
}


def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.mark.parametrize("permission", APP_PERMISSIONS)
def test_every_known_permission_is_accepted(permission: str) -> None:
    instance = {**_BASE_MANIFEST, "permissions": [permission]}
    errors = list(_validator().iter_errors(instance))
    assert not errors, f"{permission!r} should be a valid permission: {errors}"


def test_unknown_permission_is_rejected() -> None:
    instance = {**_BASE_MANIFEST, "permissions": ["shell.exec"]}
    with pytest.raises(ValidationError):
        _validator().validate(instance)


def test_is_app_permission_matches_the_enum() -> None:
    assert is_app_permission("camera.frames")
    assert is_app_permission("audio.out")
    assert not is_app_permission("shell.exec")


def test_app_permissions_is_the_documented_list() -> None:
    assert APP_PERMISSIONS == (
        "camera.frames",
        "llm.vision",
        "memory.read",
        "memory.write",
        "persons.read",
        "persons.write",
        "mail.send",
        "network.outbound",
        "body.motion",
        "body.gesture",
        "audio.in",
        "audio.out",
    )
