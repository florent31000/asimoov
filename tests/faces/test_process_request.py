"""The hook WS1 mounts serves the page and its assets, and nothing else."""

from __future__ import annotations

import pytest

from asimoov.faces.server import FACE_WS_PATH, make_process_request

hook = make_process_request()


def _header(response, name: str) -> str | None:
    return next((value for key, value in response.headers if key == name), None)


def test_face_redirects_to_directory_so_relative_assets_resolve() -> None:
    response = hook("/face?token=abc")
    assert response is not None
    assert response.status == 301
    assert _header(response, "Location") == "/face/?token=abc"


def test_index_is_served_as_html() -> None:
    response = hook("/face/")
    assert response is not None
    assert response.status == 200
    assert _header(response, "Content-Type") == "text/html; charset=utf-8"
    assert b"<canvas id=\"face\">" in response.body
    assert _header(response, "Content-Length") == str(len(response.body))


@pytest.mark.parametrize(
    ("path", "content_type", "needle"),
    [
        ("/face/face.js", "text/javascript; charset=utf-8", b"requestAnimationFrame"),
        ("/face/face.css", "text/css; charset=utf-8", b"#face"),
        ("/face/emotions.json", "application/json; charset=utf-8", b"\"neutral\""),
    ],
)
def test_assets_are_served_with_their_content_type(path, content_type, needle) -> None:
    response = hook(path)
    assert response is not None
    assert response.status == 200
    assert _header(response, "Content-Type") == content_type
    assert needle in response.body


def test_websocket_path_and_other_paths_are_left_to_the_hub() -> None:
    assert hook(FACE_WS_PATH + "?token=abc") is None
    assert hook("/bus?token=abc") is None
    assert hook("/") is None


def test_unknown_face_asset_is_404() -> None:
    response = hook("/face/../secrets.yaml")
    assert response is not None
    assert response.status == 404
