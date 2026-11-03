import asyncio
import json

import pytest
from mcp.types import CallToolResult
from tests.helpers.tradingview_market import capture

from baibai_jobs.acquisition import market_context
from baibai_jobs.acquisition.tradingview import market_daily


class Session:
    def __init__(self, *, fail_at=None):
        self.calls = []
        self.fail_at = fail_at

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if len(self.calls) == self.fail_at:
            raise RuntimeError("synthetic network error")
        if name == market_daily.BATCH_TOOL:
            payload = {
                "success": True,
                "data": {
                    s: {"close": 1, "update_mode": "delayed_streaming_900"} for s in args["symbols"]
                },
                "missing": [],
            }
            return CallToolResult(content=[], structuredContent=payload)
        sample = capture(bars=True).result.structured_content
        return CallToolResult(content=[], structuredContent={**sample, "symbol": args["symbol"]})


def test_daily_receipts_preserve_requested_symbols_and_time(tmp_path, monkeypatch):
    async def no_sleep(seconds):
        assert seconds == 15

    monkeypatch.setattr(market_daily.asyncio, "sleep", no_sleep)
    session = Session()
    directory = tmp_path / "captures"
    assert asyncio.run(market_daily.collect(session, directory)) == 3
    paths = sorted(directory.glob("*.json"))
    report = market_context.prepare(paths, tmp_path / "packet", expected_captures=3)
    assert report["sources"][0]["status"] == "ok"
    assert len(report["market_receipts"]) == 3
    value = json.loads(paths[0].read_text())
    assert value["request_parameters"]["symbols"] == ["OSE:NK2251!", "SP:SPX", "NASDAQ:NDX"]
    assert value["requested_at_utc"] <= value["received_at_utc"]
    assert (
        value["result"]["structuredContent"]["data"]["OSE:NK2251!"]["update_mode"]
        == "delayed_streaming_900"
    )


def test_interrupted_collection_preserves_partial_without_claiming_complete(tmp_path, monkeypatch):
    async def no_sleep(seconds):
        pass

    monkeypatch.setattr(market_daily.asyncio, "sleep", no_sleep)
    directory = tmp_path / "captures"
    with pytest.raises(RuntimeError, match="synthetic"):
        asyncio.run(market_daily.collect(Session(fail_at=2), directory))
    report = market_context.prepare(
        sorted(directory.glob("*.json")), tmp_path / "packet", expected_captures=3
    )
    assert report["sources"][0]["status"] == "failed"
    assert len(report["market_receipts"]) == 1
