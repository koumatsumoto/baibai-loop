from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import httpx2
import pytest
from mcp import ClientSession
from mcp.shared.message import SessionMessage
from mcp.types import (
    CallToolResult,
    JSONRPCRequest,
    JSONRPCResponse,
    ListToolsResult,
    TextContent,
    Tool,
)
from tools.diagnostics import tradingview_single_batch_probe as probe


def tool_request():
    return httpx2.Request(
        "POST",
        "https://mcp.tradingview.com/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": probe.client.BATCH_TOOL,
                "arguments": {"symbols": probe.SYMBOLS, "columns": probe.COLUMNS},
            },
        },
    )


def test_request_guard_allows_only_one_exact_tool_request():
    guard = probe.RequestGuard()
    asyncio.run(guard(tool_request()))
    with pytest.raises(probe.DiagnosticBoundaryError):
        asyncio.run(guard(tool_request()))
    assert guard.tool_requests == 1


def test_request_guard_rejects_nonempty_meta():
    payload = json.loads(tool_request().content)
    payload["params"]["_meta"] = {"unexpected": True}
    with pytest.raises(probe.DiagnosticBoundaryError):
        asyncio.run(
            probe.RequestGuard()(
                httpx2.Request("POST", "https://mcp.tradingview.com/mcp", json=payload)
            )
        )


@pytest.mark.parametrize(
    ("url", "data"),
    [
        ("https://www.tradingview.com/mcp/oauth/token", {"grant_type": "authorization_code"}),
        ("https://www.tradingview.com/mcp/oauth/register", {}),
        ("https://other.invalid/mcp", {}),
        (
            "https://www.tradingview.com/mcp/oauth/token",
            {"grant_type": "refresh_token", "scope": "new"},
        ),
        ("https://www.tradingview.com/mcp/oauth/token?unexpected=1", {}),
    ],
)
def test_guard_blocks_registration_new_grant_scope_and_other_destination(url, data):
    with pytest.raises(probe.DiagnosticBoundaryError):
        asyncio.run(probe.RequestGuard()(httpx2.Request("POST", url, data=data)))


def test_guard_allows_one_normal_refresh_only():
    guard = probe.RequestGuard()
    request = httpx2.Request(
        "POST",
        "https://www.tradingview.com/mcp/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": "fixture-private"},
    )
    asyncio.run(guard(request))
    with pytest.raises(probe.DiagnosticBoundaryError):
        asyncio.run(guard(request))
    assert guard.refresh_requests == 1


@pytest.mark.parametrize("success", [True, False])
def test_sdk_success_validation_can_list_schema_once_without_another_tool_call(
    monkeypatch, success
):
    guard = probe.RequestGuard()
    session = ClientSession(SimpleNamespace(), SimpleNamespace())
    response = CallToolResult(isError=False, content=[], structuredContent={"success": success})

    async def send(request, *args, **kwargs):
        await guard(
            httpx2.Request(
                "POST",
                "https://mcp.tradingview.com/mcp",
                json=request.model_dump(mode="json", by_alias=True, exclude_none=True),
            )
        )
        if request.method == "tools/call":
            return response
        return ListToolsResult(
            tools=[Tool(name=probe.client.BATCH_TOOL, inputSchema={"type": "object"})]
        )

    monkeypatch.setattr(session, "send_request", send)
    result = asyncio.run(
        session.call_tool(
            probe.client.BATCH_TOOL, {"symbols": probe.SYMBOLS, "columns": probe.COLUMNS}
        )
    )
    assert result is response
    assert guard.tool_requests == guard.list_requests == 1
    with pytest.raises(probe.DiagnosticBoundaryError):
        asyncio.run(
            guard(
                httpx2.Request(
                    "POST", "https://mcp.tradingview.com/mcp", json={"method": "tools/list"}
                )
            )
        )


def test_shape_contains_no_error_text_or_values():
    result = CallToolResult(
        isError=False,
        structuredContent={"success": False, "error": "HTTP 429 fixture-private"},
        content=[TextContent(type="text", text="fixture-private")],
    )
    shape = probe.response_shape(result)
    assert shape["reported_http_status"] == 429
    assert "fixture-private" not in json.dumps(shape)


def test_real_sdk_dispatcher_empty_meta_reaches_one_call_and_schema_listing():
    async def exercise():
        guard = probe.RequestGuard()
        incoming_send, incoming_receive = anyio.create_memory_object_stream(10)
        outgoing_send, outgoing_receive = anyio.create_memory_object_stream(10)

        async def peer():
            async for item in outgoing_receive:
                message = item.message
                wire = message.model_dump(mode="json", by_alias=True, exclude_unset=True)
                await guard(httpx2.Request("POST", "https://mcp.tradingview.com/mcp", json=wire))
                if not isinstance(message, JSONRPCRequest):
                    continue
                if message.method == "initialize":
                    result = {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fixture", "version": "1"},
                    }
                elif message.method == "tools/call":
                    assert wire["params"]["_meta"] == {}
                    result = {
                        "isError": False,
                        "content": [],
                        "structuredContent": {"success": True},
                    }
                else:
                    assert message.method == "tools/list"
                    assert wire["params"]["_meta"] == {}
                    result = {
                        "tools": [
                            {"name": probe.client.BATCH_TOOL, "inputSchema": {"type": "object"}}
                        ]
                    }
                await incoming_send.send(
                    SessionMessage(JSONRPCResponse(jsonrpc="2.0", id=message.id, result=result))
                )

        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(peer)
                async with ClientSession(incoming_receive, outgoing_send) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        probe.client.BATCH_TOOL,
                        {"symbols": probe.SYMBOLS, "columns": probe.COLUMNS},
                    )
                    assert result.structured_content == {"success": True}
                group.cancel_scope.cancel()
        assert guard.tool_requests == guard.list_requests == 1

    anyio.run(exercise)


@pytest.mark.parametrize("success", [True, False])
def test_live_call_once_then_memory_parse_no_store(monkeypatch, success):
    payload = (
        {
            "success": True,
            "count": 1,
            "missing_count": 0,
            "missing": [],
            "data": {"TSE:7203": dict.fromkeys(probe.COLUMNS)},
        }
        if success
        else {"success": False, "error": "HTTP 429 fixture-private"}
    )
    result = CallToolResult(
        isError=False,
        structuredContent=payload,
        content=[TextContent(type="text", text=json.dumps(payload))],
    )
    storage = SimpleNamespace(rotation_count=0)
    original_hook = probe.client._identify_request

    async def call_tool(*args, **kwargs):
        await probe.client._identify_request(tool_request())
        return result

    session = SimpleNamespace(call_tool=AsyncMock(side_effect=call_tool))

    @asynccontextmanager
    async def connect(received):
        assert received is storage
        yield session

    def forbid_store(*args, **kwargs):
        raise AssertionError("Unexpected SQLite access")

    monkeypatch.setattr(probe, "credential_storage", lambda _: storage)
    monkeypatch.setattr(probe.client, "connect", connect)
    monkeypatch.setattr("sqlite3.connect", forbid_store)
    status, report = asyncio.run(probe.run())
    assert status == (0 if success else 1)
    session.call_tool.assert_awaited_once_with(
        probe.client.BATCH_TOOL, {"symbols": probe.SYMBOLS, "columns": probe.COLUMNS}
    )
    assert report["tool_requests"] == 1
    assert report["refresh_requests"] == report["oauth_rotations"] == 0
    assert probe.client._identify_request is original_hook
    assert "fixture-private" not in json.dumps(report)
    if success:
        assert report["normalized"]
        assert report["rows"] == 1
    else:
        assert "provider_rate_limit" in report["failure"]
