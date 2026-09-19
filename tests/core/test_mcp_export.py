"""The minimal MCP server over the tool registry."""

from __future__ import annotations

import io
import json

from asimoov.contracts.behaviors import BehaviorStatus
from asimoov.contracts.tools import ToolResult, ToolSpec
from asimoov.core.tools.mcp_export import PROTOCOL_VERSION, McpServer, serve_stdio
from asimoov.core.tools.registry import ToolRegistry


def registry_with_one_tool(status=BehaviorStatus.OK):
    registry = ToolRegistry()

    async def handler(params):
        return ToolResult(status=status, content={"echo": params}, reason="because" if status is BehaviorStatus.ERROR else None)

    registry.register(
        ToolSpec(
            name="who_is_here",
            description="List people",
            params={"type": "object", "properties": {}},
        ),
        handler,
    )
    return registry


def request(method, params=None, request_id=1):
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


async def test_initialize_announces_tools():
    server = McpServer(registry_with_one_tool())
    response = await server.handle(request("initialize"))
    assert response["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert response["result"]["capabilities"] == {"tools": {}}
    assert response["result"]["serverInfo"]["name"] == "asimoov"


async def test_tools_list_exposes_the_input_schema():
    server = McpServer(registry_with_one_tool())
    response = await server.handle(request("tools/list"))
    tool = response["result"]["tools"][0]
    assert tool["name"] == "who_is_here"
    assert tool["inputSchema"] == {"type": "object", "properties": {}}


async def test_tools_call_returns_the_tool_result_as_text():
    server = McpServer(registry_with_one_tool())
    response = await server.handle(
        request("tools/call", {"name": "who_is_here", "arguments": {"x": 1}})
    )
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["status"] == "ok"
    assert payload["content"] == {"echo": {"x": 1}}
    assert response["result"]["isError"] is False


async def test_a_failing_tool_is_flagged_as_an_error():
    server = McpServer(registry_with_one_tool(status=BehaviorStatus.ERROR))
    response = await server.handle(request("tools/call", {"name": "who_is_here"}))
    assert response["result"]["isError"] is True


async def test_bad_params_are_rejected():
    server = McpServer(registry_with_one_tool())
    response = await server.handle(request("tools/call", {"arguments": {}}))
    assert response["error"]["code"] == -32602


async def test_an_unknown_method_is_rejected():
    server = McpServer(registry_with_one_tool())
    response = await server.handle(request("resources/list"))
    assert response["error"]["code"] == -32601


async def test_notifications_get_no_answer():
    server = McpServer(registry_with_one_tool())
    assert await server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


async def test_ping():
    server = McpServer(registry_with_one_tool())
    assert (await server.handle(request("ping")))["result"] == {}


async def test_serve_stdio_reads_lines_until_eof():
    stdin = io.StringIO(
        json.dumps(request("initialize")) + "\n"
        "not json\n"
        "\n" + json.dumps(request("tools/list", request_id=2)) + "\n"
    )
    stdout = io.StringIO()
    await serve_stdio(registry_with_one_tool(), stdin=stdin, stdout=stdout)
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [response["id"] for response in responses] == [1, 2]
