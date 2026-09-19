"""Expose the tool registry as an MCP server over stdio (`asimoov mcp`).

JSON-RPC 2.0 written by hand on purpose: the core install may not grow a
dependency for this (plan.md section 4.10), and the surface needed --
`initialize`, `tools/list`, `tools/call`, `ping` -- is a hundred lines.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from asimoov.core.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "asimoov"
SERVER_VERSION = "0.1.0"

METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class McpServer:
    """Answers MCP requests from a `ToolRegistry`."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Handle one JSON-RPC message; returns None for notifications."""
        method = request.get("method", "")
        request_id = request.get("id")
        if request_id is None:
            return None

        if method == "initialize":
            return self._ok(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            )
        if method == "ping":
            return self._ok(request_id, {})
        if method == "tools/list":
            return self._ok(request_id, {"tools": [self._tool(spec) for spec in self.registry.specs()]})
        if method == "tools/call":
            return await self._call(request_id, request.get("params") or {})
        return self._error(request_id, METHOD_NOT_FOUND, f"unknown method {method!r}")

    async def _call(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            return self._error(request_id, INVALID_PARAMS, "params.name must be a string")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return self._error(request_id, INVALID_PARAMS, "params.arguments must be an object")

        result = await self.registry.call(name, arguments)
        payload = result.to_dict()
        return self._ok(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "isError": payload["status"] in ("error", "timeout", "unsupported"),
            },
        )

    @staticmethod
    def _tool(spec) -> dict[str, Any]:
        return {
            "name": spec.name,
            "description": spec.description,
            "inputSchema": spec.params,
        }

    @staticmethod
    def _ok(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


async def serve_stdio(registry: ToolRegistry, *, stdin=None, stdout=None) -> None:
    """Serve MCP on line-delimited JSON over stdin/stdout until EOF."""
    server = McpServer(registry)
    reader = stdin or sys.stdin
    writer = stdout or sys.stdout
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, reader.readline)
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            log.warning("mcp: ignoring a non-JSON line")
            continue
        response = await server.handle(request)
        if response is None:
            continue
        writer.write(json.dumps(response) + "\n")
        writer.flush()
