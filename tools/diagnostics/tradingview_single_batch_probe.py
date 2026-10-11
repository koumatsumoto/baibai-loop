"""Temporary #1386 comparison; remove after the single authorized Actions run."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from importlib.metadata import version
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs

import httpx2
from mcp import ClientSession
from mcp.types import CallToolResult

from baibai_engine.market.tradingview import client
from baibai_engine.market.tradingview.cli import credential_storage, failure_line
from baibai_engine.market.tradingview.observations import COLUMNS, normalize_batch

SYMBOLS = ["TSE:7203"]


class DiagnosticBoundaryError(RuntimeError):
    """A request outside the single approved comparison was stopped before send."""


class RequestGuard:
    def __init__(self) -> None:
        self.tool_requests = 0
        self.refresh_requests = 0
        self.list_requests = 0

    async def __call__(self, request: httpx2.Request) -> None:
        url = request.url
        if url.scheme != "https" or url.query or url.fragment:
            raise DiagnosticBoundaryError()
        if url.host == "mcp.tradingview.com" and url.path == "/mcp":
            if request.method in {"GET", "DELETE"}:
                return
            if request.method == "POST":
                payload = json.loads(request.content)
                method = payload.get("method")
                if method in {"initialize", "notifications/initialized", "notifications/cancelled"}:
                    return
                if method == "tools/list" and not payload.get("params") and not self.list_requests:
                    self.list_requests += 1
                    return
                if method == "tools/call" and payload.get("params") == {
                    "name": client.BATCH_TOOL,
                    "arguments": {"symbols": SYMBOLS, "columns": COLUMNS},
                }:
                    if self.tool_requests:
                        raise DiagnosticBoundaryError()
                    self.tool_requests += 1
                    return
        if url.host == "www.tradingview.com":
            if request.method == "GET" and url.path.startswith("/.well-known/"):
                return
            if request.method == "POST" and url.path == "/mcp/oauth/token":
                form = parse_qs(request.content.decode())
                if form.get("grant_type") == ["refresh_token"] and not self.refresh_requests:
                    if "scope" in form:
                        raise DiagnosticBoundaryError()
                    self.refresh_requests += 1
                    return
        raise DiagnosticBoundaryError()


def response_shape(result: CallToolResult) -> dict[str, Any]:
    structured = result.structured_content
    texts = [item.text for item in result.content if item.type == "text"]
    parsed: Any = None
    parseable = False
    if len(texts) == 1:
        try:
            parsed = json.loads(texts[0])
            parseable = True
        except ValueError:
            pass
    payload = structured if structured is not None else parsed
    error = payload.get("error") if isinstance(payload, dict) else None
    status_texts = [error] if isinstance(error, str) else texts if result.is_error else []
    return {
        "is_error": result.is_error,
        "structured_present": structured is not None,
        "structured_is_object": isinstance(structured, dict),
        "text_items": len(texts),
        "text_json_parseable": parseable,
        "structured_text_equal": structured == parsed if parseable else None,
        "success": payload.get("success")
        if isinstance(payload, dict) and type(payload.get("success")) is bool
        else None,
        "error_is_string": isinstance(error, str),
        "error_mentions_scanner": "scanner.tradingview.com" in error
        if isinstance(error, str)
        else False,
        "reported_http_status": client._provider_http_status(status_texts),
    }


async def run() -> tuple[int, dict[str, Any]]:
    guard = RequestGuard()
    original_hook = client._identify_request
    storage = None
    report: dict[str, Any] = {
        "started_at_utc": datetime.now(UTC).isoformat(),
        "sdk_version": version("mcp"),
        "columns_count": len(COLUMNS),
        "db_writes": 0,
        "phase": "credentials",
    }

    async def guarded_hook(request: httpx2.Request) -> None:
        await guard(request)
        await original_hook(request)

    client._identify_request = guarded_hook
    try:
        storage = credential_storage(None)
        report["phase"] = "connect"
        async with asyncio.timeout(120), client.connect(storage) as session:
            report["phase"] = "fetch"
            result = await session.call_tool(
                client.BATCH_TOOL, {"symbols": SYMBOLS, "columns": COLUMNS}
            )
            report["response"] = response_shape(result)

            async def replay(*args: Any, **kwargs: Any) -> CallToolResult:
                return result

            report["phase"] = "normalize"
            parsed = await client.fetch_batch(
                cast(ClientSession, SimpleNamespace(call_tool=replay)), SYMBOLS, COLUMNS
            )
            rows = normalize_batch(parsed, SYMBOLS, datetime.now(UTC))
            report.update(status="ok", rows=len(rows), normalized=True)
        return 0, report
    except Exception as exc:
        report.update(status="failed", failure=failure_line(exc))
        return 1, report
    finally:
        client._identify_request = original_hook
        report.update(
            finished_at_utc=datetime.now(UTC).isoformat(),
            tool_requests=guard.tool_requests,
            list_requests=guard.list_requests,
            refresh_requests=guard.refresh_requests,
            oauth_rotations=storage.rotation_count if storage is not None else 0,
        )


def main() -> int:
    # SDK exception logging can include response bodies; only our fixed report is public.
    logging.disable(logging.CRITICAL)
    status, report = asyncio.run(run())
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
