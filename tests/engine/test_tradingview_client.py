from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent

from baibai_engine.market.tradingview.client import BATCH_TOOL, fetch_batch

SCANNER_ERROR = (
    "tradingview api: https://scanner.tradingview.com/japan/scan?label-product=tv-mcp: 429"
)
SCANNER_ERROR_WITH_SUFFIX = SCANNER_ERROR + ": "


def test_official_batch_tool_preserves_raw_response() -> None:
    data = {"success": True, "count": 1, "data": {"TSE:7203": {"close": None}}}
    session = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(
                is_error=False,
                structured_content=data,
            )
        )
    )
    assert asyncio.run(fetch_batch(session, ["TSE:7203"], ["close"])) is data
    session.call_tool.assert_awaited_once_with(
        BATCH_TOOL, {"symbols": ["TSE:7203"], "columns": ["close"]}
    )


@pytest.mark.parametrize("symbols", [[], ["TSE:7203"] * 2, [f"TSE:{1000 + i}" for i in range(51)]])
def test_invalid_chunk_never_calls_provider(symbols) -> None:
    session = SimpleNamespace(call_tool=AsyncMock())
    with pytest.raises(ValueError, match="unique symbols"):
        asyncio.run(fetch_batch(session, symbols, ["close"]))
    session.call_tool.assert_not_called()


def test_tool_error_is_not_converted_to_missing() -> None:
    session = SimpleNamespace(
        call_tool=AsyncMock(return_value=SimpleNamespace(is_error=True, content=[]))
    )
    with pytest.raises(RuntimeError, match="tool failed"):
        asyncio.run(fetch_batch(session, ["TSE:7203"], ["close"]))


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("HTTP 429 private-token https://secret", 429),
        ("status code 503 private-token", 503),
        ("status 401", 401),
        ("HTTP 403 HTTP 403", 403),
        ("429 private-token", None),
        ("HTTP 429 status code 503 private-token", None),
        ("HTTP 999", None),
        ("HTTP 4290", None),
        (SCANNER_ERROR, 429),
        (SCANNER_ERROR_WITH_SUFFIX, 429),
    ],
)
def test_tool_status_extraction_does_not_retry_or_disclose(text, status):
    from baibai_engine.market.tradingview.observations import FetchError, ProviderHTTPError

    session = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=CallToolResult(isError=True, content=[TextContent(type="text", text=text)])
        )
    )
    with pytest.raises(FetchError) as caught:
        asyncio.run(fetch_batch(session, ["TSE:7203"], ["close"]))
    assert getattr(caught.value, "status_code", None) == status
    assert isinstance(caught.value, ProviderHTTPError) == (status is not None)
    assert "private-token" not in str(caught.value)
    assert "https://secret" not in str(caught.value)
    session.call_tool.assert_awaited_once()


@pytest.mark.parametrize("structured", [True, False])
@pytest.mark.parametrize(
    "error", [SCANNER_ERROR, SCANNER_ERROR_WITH_SUFFIX, "HTTP 429 private-token https://secret"]
)
def test_unsuccessful_envelope_reports_safe_status_without_retry(structured, error):
    from baibai_engine.market.tradingview.cli import failure_line
    from baibai_engine.market.tradingview.observations import ProviderHTTPError

    payload = {"success": False, "error": error, "token": "private-token"}
    session = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=CallToolResult(
                isError=False,
                structuredContent=payload if structured else None,
                content=[TextContent(type="text", text=json.dumps(payload))],
            )
        )
    )
    with pytest.raises(ProviderHTTPError) as caught:
        asyncio.run(fetch_batch(session, ["TSE:7203"], ["close"]))
    assert caught.value.status_code == 429
    assert failure_line(caught.value) == (
        "TradingView acquisition failed; category=provider_rate_limit; provider_http_status=429"
    )
    assert error not in str(caught.value)
    assert "private" not in str(caught.value)
    session.call_tool.assert_awaited_once()


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "error": "429 private-token"},
        {"success": False, "error": "HTTP 429 status code 503"},
        {"success": False, "error": {"message": "HTTP 429"}},
        {"success": False, "error": SCANNER_ERROR.replace("scanner.tradingview.com", "secret")},
        {"success": False, "error": SCANNER_ERROR + "0"},
        {"success": False, "error": SCANNER_ERROR.replace(": 429", ": 200")},
        {"success": False, "message": "HTTP 429"},
        {"error": "HTTP 429"},
        {"success": 0, "error": "HTTP 429"},
    ],
)
def test_unrecognized_envelope_stays_invalid_without_text_fallback(payload):
    from baibai_engine.market.tradingview.observations import ProviderPayloadError

    session = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=CallToolResult(
                isError=False,
                structuredContent=payload,
                content=[TextContent(type="text", text='{"success":true,"data":{}}')],
            )
        )
    )
    with pytest.raises(ProviderPayloadError) as caught:
        asyncio.run(fetch_batch(session, ["TSE:7203"], ["close"]))
    assert caught.value.reason == "malformed_envelope"
    session.call_tool.assert_awaited_once()


def test_successful_envelope_does_not_extract_status_from_error_field():
    payload = {"success": True, "data": {}, "error": SCANNER_ERROR}
    session = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=CallToolResult(isError=False, structuredContent=payload, content=[])
        )
    )
    assert asyncio.run(fetch_batch(session, ["TSE:7203"], ["close"])) == payload
