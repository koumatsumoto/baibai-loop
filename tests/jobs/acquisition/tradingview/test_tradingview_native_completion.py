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
from baibai_jobs.acquisition.inputs import export_source_input
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
@pytest.mark.parametrize("legacy", [False, True])
def test_invalid_or_different_request_does_not_fulfil_native_work(tmp_path, damage, legacy):
    path = store(tmp_path / "market.sqlite", 1)
    if legacy:
        legacy_projection(path, ["TSE:1000"])
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


def test_scoped_input_keeps_current_native_receipts_without_old_history(tmp_path):
    path = store(tmp_path / "canonical.sqlite", 1)
    response = payload(["TSE:1000"])
    response["data"]["TSE:1000"]["close"] = "not a number"
    current = retain(path, response)
    retain(path, response, snapshot_date=DAY - timedelta(days=1))
    scoped = tmp_path / "scoped.sqlite"
    report = export_source_input(path, scoped, group="tradingview", asof=DAY)
    assert report["rows"]["tradingview_source_receipts"] == 1
    with closing(open_connection(scoped)) as conn:
        assert conn.execute("SELECT receipt_id FROM tradingview_source_receipts").fetchall() == [
            (current,)
        ]
    state = preflight_snapshot(scoped, DAY, now=NOW)
    assert state["status"] == "originals_saved"
    assert state["projection_pending"] == 1
    assert state["remaining_requests"] == 0


def legacy_projection(path, symbols):
    """Represent an old/incomplete copy with L2 but no corresponding receipt."""
    from tests.helpers.tradingview import seed_snapshot

    seed_snapshot(path, symbols)
    with closing(open_connection(path)) as conn, conn:
        conn.execute("DELETE FROM tradingview_source_receipts")


def test_l2_only_current_cycle_receives_originals_without_replacing_projection(tmp_path):
    from tests.helpers.tradingview import stored

    from baibai_jobs.maintenance.tradingview_rebuild import rebuild_from_receipts

    path = store(tmp_path / "market.sqlite", 2)
    legacy_projection(path, ["TSE:1000", "TSE:1001"])
    before = stored(path)
    before_bytes = path.read_bytes()
    canonical = tmp_path / "canonical.sqlite"
    canonical.write_bytes(before_bytes)
    state = preflight_snapshot(path, DAY, now=NOW)
    assert path.read_bytes() == before_bytes
    assert state["status"] == "needs_fetch"
    assert state["remaining"] == state["projection_pending"] == 0
    assert state["remaining_requests"] == state["rows_without_originals"] == 2
    assert state["native_received"] == 0
    calls = []
    received = NOW + timedelta(minutes=1)

    async def fetch(symbols, columns):
        calls.append(symbols)
        response = payload(symbols)
        for row in response["data"].values():
            row["close"] = 999
        return CallToolResult(content=[], structuredContent=response)

    result = asyncio.run(collect(path, DAY, fetch, clock=lambda: received))
    assert calls == [["TSE:1000", "TSE:1001"]]
    assert result["status"] == "saved"
    assert result["rows_added"] == result["remaining_requests"] == 0
    assert result["native_added"] == result["native_received"] == 2
    assert stored(path) == before
    assert (
        asyncio.run(collect(path, DAY, fetch, clock=lambda: received))["status"] == "already_saved"
    )
    assert len(calls) == 1
    with closing(open_connection(path)) as conn:
        receipts = conn.execute(
            "SELECT receipt_id, received_at_utc FROM tradingview_source_receipts"
        ).fetchall()
    assert len(receipts) == 1
    assert receipts[0][1] == received.isoformat(timespec="microseconds")
    from baibai_jobs.integration.tradingview_handoff import export_handoff, import_handoff

    stage = tmp_path / "stage.sqlite"
    selected = export_handoff(path, stage, day=DAY, since=received)
    imported = import_handoff(stage, canonical, selected)
    assert imported.complete
    assert imported.report["raw_receipts_added"] == 1
    assert imported.report["rows_added"] == 0
    assert stored(canonical) == before
    assert preflight_snapshot(canonical, DAY, now=received)["status"] == "already_saved"
    # The new original is not retroactively the basis of the retained L2 row.
    # Only the existing explicit rebuild selects it as the new calculation input.
    assert rebuild_from_receipts(path, DAY, [receipts[0][0]])["rows"] == 2
    assert stored(path) != before


def test_mixed_native_legacy_and_unreceived_symbols_keep_separate_counts(tmp_path):
    from tests.helpers.tradingview import stored

    path = store(tmp_path / "market.sqlite", 3)
    legacy_projection(path, ["TSE:1001"])
    before = stored(path)
    response = payload(["TSE:1000"])
    response["data"]["TSE:1000"]["close"] = "not a number"
    retain(path, response)
    state = preflight_snapshot(path, DAY, now=NOW)
    assert state["remaining"] == state["remaining_requests"] == 2
    assert state["projection_pending"] == state["rows_without_originals"] == 1
    calls = []

    async def fetch(symbols, columns):
        calls.append(symbols)
        return CallToolResult(content=[], structuredContent=payload(symbols))

    result = asyncio.run(collect(path, DAY, fetch, clock=lambda: NOW))
    assert calls == [["TSE:1001", "TSE:1002"]]
    assert result["status"] == "partial"
    assert result["native_received"] == 3
    assert result["native_added"] == 2
    assert result["rows_added"] == result["remaining"] == result["projection_pending"] == 1
    assert result["remaining_requests"] == result["rows_without_originals"] == 0
    assert stored(path)[:1] == before
    assert preflight_snapshot(path, DAY, now=NOW)["status"] == "originals_saved"


def test_l2_complete_does_not_hide_partial_or_failed_original_acquisition(tmp_path):
    from tests.helpers.tradingview import stored

    from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
        ProviderHTTPError,
    )
    from baibai_jobs.observability.tradingview import CollectionFailure

    path = store(tmp_path / "market.sqlite", 2)
    legacy_projection(path, ["TSE:1000", "TSE:1001"])
    before = stored(path)

    async def fetch(symbols, columns):
        if symbols == ["TSE:1001"]:
            raise ProviderHTTPError(429)
        return CallToolResult(content=[], structuredContent=payload(symbols))

    result = asyncio.run(collect(path, DAY, fetch, batch_size=1, interval=0, clock=lambda: NOW))
    assert result["status"] == "partial"
    assert result["rows_added"] == result["remaining"] == 0
    assert result["native_added"] == result["remaining_requests"] == 1
    assert result["rows_without_originals"] == 1
    assert result["stop_reason"] == "provider_rate_limit"
    assert stored(path) == before
    with pytest.raises(CollectionFailure):
        asyncio.run(collect(path, DAY, fetch, batch_size=1, interval=0, clock=lambda: NOW))
    assert preflight_snapshot(path, DAY, now=NOW)["status"] == "needs_fetch"
    assert stored(path) == before


def test_old_cycle_l2_only_rows_do_not_authorize_current_response_backfill(tmp_path):
    from baibai_jobs.observability.tradingview import TimeGuardError

    path = store(tmp_path / "market.sqlite", 1)
    legacy_projection(path, ["TSE:1000"])

    async def forbidden(*_):
        raise AssertionError("old cycle cannot be repaired with a current provider request")

    with pytest.raises(TimeGuardError, match="current acquisition cycle"):
        preflight_snapshot(path, DAY, now=NOW + timedelta(days=1))
    with pytest.raises(TimeGuardError):
        asyncio.run(collect(path, DAY, forbidden, clock=lambda: NOW + timedelta(days=1)))
    with closing(open_connection(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tradingview_source_receipts").fetchone()[0] == 0


def test_new_concurrent_projection_is_not_mistaken_for_a_preexisting_legacy_row(tmp_path):
    from tests.helpers.tradingview import stored

    from baibai_jobs.observability.tradingview import CollectionFailure

    path = store(tmp_path / "market.sqlite", 2)
    legacy_projection(path, ["TSE:1000"])
    before = stored(path)

    async def fetch(symbols, columns):
        with closing(open_connection(path)) as conn, conn:
            conn.execute(
                "INSERT INTO tradingview_forecast_snapshots "
                "(snapshot_date,ticker,provider_symbol,fetched_at_utc,fetch_status) "
                "VALUES (?,?,?,?,?)",
                (DAY.isoformat(), "1001", "TSE:1001", NOW.isoformat(), "ok"),
            )
        return CallToolResult(content=[], structuredContent=payload(symbols))

    with pytest.raises(CollectionFailure) as failed:
        asyncio.run(collect(path, DAY, fetch, clock=lambda: NOW))
    assert "changed during collection" in str(failed.value.cause)
    assert stored(path)[:1] == before
    with closing(open_connection(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tradingview_source_receipts").fetchone()[0] == 1
