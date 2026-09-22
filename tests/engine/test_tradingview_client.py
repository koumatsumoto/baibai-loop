from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from baibai_engine.market.tradingview.client import BATCH_TOOL, fetch_batch


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
    session = SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(is_error=True)))
    with pytest.raises(RuntimeError, match="tool failed"):
        asyncio.run(fetch_batch(session, ["TSE:7203"], ["close"]))
