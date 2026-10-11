"""Temporary #1386 fixed-50 comparison; remove after the authorized Actions run."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from importlib.metadata import version
from time import monotonic
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs

import httpx2
from mcp import ClientSession
from mcp.types import CallToolResult

from baibai_engine.market.tradingview import client
from baibai_engine.market.tradingview.cli import credential_storage, failure_line
from baibai_engine.market.tradingview.observations import COLUMNS, FIELDS, normalize_batch

SYMBOLS = [
    "TSE:" + ticker
    for ticker in [
        "1301",
        "130A",
        "1332",
        "1333",
        "135A",
        "1375",
        "1376",
        "1377",
        "1379",
        "137A",
        "1380",
        "1381",
        "1382",
        "1383",
        "1384",
        "138A",
        "1401",
        "1407",
        "1414",
        "1417",
        "1418",
        "1419",
        "141A",
        "1420",
        "1429",
        "142A",
        "1430",
        "1431",
        "1433",
        "1434",
        "1435",
        "1436",
        "1438",
        "143A",
        "1443",
        "1444",
        "1446",
        "1447",
        "1450",
        "145A",
        "146A",
        "147A",
        "148A",
        "1491",
        "149A",
        "150A",
        "1514",
        "1515",
        "1518",
        "151A",
    ]
]
MAX_CALLS = 2
INTERVAL_SECONDS = 90


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
                params = payload.get("params") or {}
                # The SDK dispatcher inserts empty _meta even without progress callbacks.
                if params.get("_meta") not in (None, {}):
                    raise DiagnosticBoundaryError()
                params = {key: value for key, value in params.items() if key != "_meta"}
                if method in {"initialize", "notifications/initialized", "notifications/cancelled"}:
                    return
                if method == "tools/list" and not params and not self.list_requests:
                    self.list_requests += 1
                    return
                if method == "tools/call" and params == {
                    "name": client.BATCH_TOOL,
                    "arguments": {"symbols": SYMBOLS, "columns": COLUMNS},
                }:
                    if self.tool_requests >= MAX_CALLS:
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
        report["attempts"] = []
        async with asyncio.timeout(360), client.connect(storage) as session:
            for attempt_index in range(MAX_CALLS):
                if attempt_index:
                    await asyncio.sleep(INTERVAL_SECONDS)
                attempt: dict[str, Any] = {
                    "call_number": 5 + attempt_index,
                    "started_at_utc": datetime.now(UTC).isoformat(),
                }
                report["attempts"].append(attempt)
                started = monotonic()
                try:
                    report["phase"] = "fetch"
                    result = await session.call_tool(
                        client.BATCH_TOOL, {"symbols": SYMBOLS, "columns": COLUMNS}
                    )
                    attempt["latency_seconds"] = round(monotonic() - started, 3)
                    attempt["response"] = response_shape(result)

                    async def replay(
                        *args: Any, response: CallToolResult = result, **kwargs: Any
                    ) -> CallToolResult:
                        return response

                    report["phase"] = "normalize"
                    parsed = await client.fetch_batch(
                        cast(ClientSession, SimpleNamespace(call_tool=replay)), SYMBOLS, COLUMNS
                    )
                    rows = normalize_batch(parsed, SYMBOLS, datetime.now(UTC))
                    attempt.update(
                        status="ok",
                        rows=len(rows),
                        normalized=True,
                        unresolved_rows=sum(row["fetch_status"] == "unresolved" for row in rows),
                        null_cells=sum(row[key] is None for row in rows for key in FIELDS),
                        no_rating_rows=sum(row["analyst_rating"] == "NoRating" for row in rows),
                        all_numeric_forecasts_null_rows=sum(
                            all(
                                row[key] is None
                                for key in FIELDS
                                if key not in {"analyst_rating", "quote_close", "quote_currency"}
                            )
                            for row in rows
                        ),
                    )
                except Exception as exc:
                    attempt.update(status="failed", failure=failure_line(exc))
                    raise
                finally:
                    attempt["finished_at_utc"] = datetime.now(UTC).isoformat()
                    attempt.setdefault("latency_seconds", round(monotonic() - started, 3))
            report.update(status="ok", normalized=True)
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
