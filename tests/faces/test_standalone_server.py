"""The standalone runner (`python -m asimoov.faces.web --demo`) end to end."""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from asimoov.contracts.vocab import EMOTIONS
from asimoov.faces.server import WebFace, demo_state, serve_face

TOKEN = "test-token"


@pytest.fixture
async def server():
    face = WebFace()
    running = await serve_face(face, host="127.0.0.1", port=0, token=TOKEN)
    port = running.sockets[0].getsockname()[1]
    try:
        yield face, port
    finally:
        running.close()
        await running.wait_closed()
        await face.stop()


async def _http_get(port: int, path: str) -> tuple[str, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read(-1)
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return head.decode("latin-1"), body.decode("utf-8", "replace")


async def test_the_page_and_its_assets_are_served_on_the_websocket_port(server) -> None:
    _face, port = server

    head, body = await _http_get(port, "/face/")
    assert head.startswith("HTTP/1.1 200")
    assert "text/html" in head
    assert "<canvas id=\"face\">" in body

    head, _ = await _http_get(port, "/face")
    assert head.startswith("HTTP/1.1 301")
    assert "Location: /face/" in head

    head, body = await _http_get(port, "/face/emotions.json")
    assert head.startswith("HTTP/1.1 200")
    assert tuple(json.loads(body)["emotions"]) == EMOTIONS


async def test_a_page_receives_the_demo_states(server) -> None:
    face, port = server
    async with connect(f"ws://127.0.0.1:{port}/face/ws?token={TOKEN}") as page:
        await face.render(demo_state(0.0))
        envelope = json.loads(await asyncio.wait_for(page.recv(), 2.0))
        assert envelope["topic"] == "face.state"
        assert envelope["data"]["emotion"] in EMOTIONS


async def test_a_page_with_a_wrong_token_is_rejected(server) -> None:
    _face, port = server
    async with connect(f"ws://127.0.0.1:{port}/face/ws?token=nope") as page:
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(page.recv(), 2.0)
