"""Native receipt completion does not depend on successful numeric projection."""

import asyncio
from contextlib import closing
from datetime import timedelta

import pytest
from mcp.types import CallToolResult
from tests.helpers.tradingview import DAY, NOW, payload, store

from baibai_engine.data_collection.providers.tradingview.contract import BATCH_TOOL
from baibai_engine.data_collection.providers.tradingview.receipts import retain_tool_result
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
    COLUMNS,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.tradingview.collector import collect, preflight_snapshot


def retain(path, response, *, symbols=("TSE:1000",), **overrides):
    values = {
        "snapshot_date": DAY,
        "master_asof": DAY,
        "tool_name": BATCH_TOOL,
        "received_at_utc": NOW,
        "requested_symbols": symbols,
        "requested_columns": COLUMNS,
        "result": CallToolResult(content=[], structuredContent=response),
        **overrides,
    }
    with closing(open_connection(path)) as conn:
        return retain_tool_result(conn, **values)


def test_complete_original_with_invalid_number_is_not_refetched(tmp_path):
    path = store(tmp_path / "market.sqlite", 1)
    response = payload(["TSE:1000"])
    response["data"]["TSE:1000"]["close"] = "not a number"
    retain(path, response)
    state = preflight_snapshot(path, DAY, now=NOW)
    assert state["status"] == "originals_saved"
    assert state["rows"] == 0
    assert state["remaining"] == state["projection_pending"] == 1
    assert state["remaining_requests"] == state["requests_attempted"] == 0

    async def forbidden(*_):
        raise AssertionError("retained native answer must not be replaced by a GET")

    result = asyncio.run(collect(path, DAY, forbidden, clock=lambda: NOW))
    assert result["status"] == "originals_saved"
    with closing(open_connection(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tradingview_source_receipts").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM tradingview_forecast_snapshots").fetchone()[0] == 0
        )


def test_partial_native_completion_fetches_only_missing_request(tmp_path):
    path = store(tmp_path / "market.sqlite", 2)
    response = payload(["TSE:1000"])
    response["data"]["TSE:1000"]["close"] = "not a number"
    retain(path, response)
    calls = []

    async def fetch(symbols, columns):
        calls.append(symbols)
        return CallToolResult(content=[], structuredContent=payload(symbols))

    result = asyncio.run(collect(path, DAY, fetch, clock=lambda: NOW))
    assert calls == [["TSE:1001"]]
    assert result["expected_universe"] == 2
    assert result["rows"] == result["remaining"] == 1
    assert result["status"] == "partial"
    assert result["remaining_requests"] == 0
    assert result["projection_pending"] == 1
    state = preflight_snapshot(path, DAY, now=NOW)
    assert state["status"] == "originals_saved"
    assert state["remaining_requests"] == 0
    assert state["projection_pending"] == 1


@pytest.mark.parametrize("damage", ["http", "count", "columns", "master", "time", "tool", "field"])
def test_invalid_or_different_request_does_not_fulfil_native_work(tmp_path, damage):
    path = store(tmp_path / "market.sqlite", 1)
    response = payload(["TSE:1000"])
    overrides = {}
    if damage == "http":
        overrides["result"] = CallToolResult(isError=True, content=[])
    elif damage == "count":
        response["count"] = 2
    elif damage == "columns":
        overrides["requested_columns"] = COLUMNS[:-1]
    elif damage == "master":
        overrides["master_asof"] = DAY - timedelta(days=1)
    elif damage == "time":
        overrides["received_at_utc"] = NOW - timedelta(days=1)
    elif damage == "tool":
        overrides["tool_name"] = "other-tool"
    else:
        response["data"]["TSE:1000"].pop("close")
    retain(path, response, **overrides)
    state = preflight_snapshot(path, DAY, now=NOW)
    assert state["status"] == "needs_fetch"
    assert state["remaining_requests"] == 1
    assert state["projection_pending"] == 0


def test_explicit_native_missing_is_received_but_not_a_forecast(tmp_path):
    path = store(tmp_path / "market.sqlite", 1)
    retain(path, payload(["TSE:1000"], missing=["TSE:1000"]))
    state = preflight_snapshot(path, DAY, now=NOW)
    assert state["status"] == "originals_saved"
    assert state["native_received"] == 1
    assert state["rows"] == 0
