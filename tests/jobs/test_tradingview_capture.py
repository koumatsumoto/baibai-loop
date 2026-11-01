"""Explicit native receipt import retains original results before offline validation."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from mcp.types import CallToolResult
from tests.helpers.tradingview import DAY, payload, store

from baibai_engine.data_collection.providers.tradingview.contract import (
    BATCH_TOOL,
    OFFICIAL_CONNECTOR_BATCH_TOOL,
)
from baibai_engine.data_collection.providers.tradingview.receipts import read_tool_results
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
    COLUMNS,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.tradingview_handoff import export_handoff, import_handoff
from baibai_jobs.maintenance.tradingview_capture import retain_capture
from baibai_jobs.acquisition.tradingview.cli import main
from baibai_jobs.maintenance.tradingview_rebuild import RebuildError, rebuild_from_receipts
from baibai_jobs.observability.tradingview import SourceDataError

SATURDAY = DAY + timedelta(days=1)
RECEIVED = datetime(2026, 9, 19, 0, 5, tzinfo=UTC)


def weekend_store(path):
    store(path, 3)
    with closing(open_connection(path)) as conn, conn:
        conn.execute("INSERT INTO jquants_market_calendar VALUES (?,0)", (SATURDAY.isoformat(),))
    return path


def capture_file(
    tmp_path,
    *,
    result=None,
    identity="synthetic-call",
    symbols=("TSE:1000",),
    tool_name=BATCH_TOOL,
):
    if result is None:
        result = CallToolResult(isError=False, content=[], structuredContent=payload(list(symbols)))
    value = {
        "receipt_id": identity,
        "snapshot_date": SATURDAY.isoformat(),
        "master_asof": DAY.isoformat(),
        "tool_name": tool_name,
        "received_at_utc": RECEIVED.isoformat(),
        "requested_symbols": list(symbols),
        "requested_columns": COLUMNS,
        "result": result.model_dump(mode="json", by_alias=True),
    }
    path = tmp_path / (identity + ".json")
    path.write_text(json.dumps(value, ensure_ascii=False))
    return path


def test_receive_cli_keeps_native_result_before_weekend_partial_handoff(tmp_path, capsys):
    source = weekend_store(tmp_path / "source.sqlite")
    canonical = weekend_store(tmp_path / "canonical.sqlite")
    symbols = ("TSE:1000", "TSE:1001")
    body = payload(list(symbols), missing=["TSE:1001"])
    body["unknown_provider_metadata"] = {"unit": None, "period": None, "original": "そのまま"}
    result = CallToolResult.model_validate(
        {
            "_meta": {"native": True},
            "isError": False,
            "content": [{"type": "text", "text": "native\nreceipt"}],
            "structuredContent": body,
        }
    )
    captured = capture_file(
        tmp_path, result=result, symbols=symbols, tool_name=OFFICIAL_CONNECTOR_BATCH_TOOL
    )
    assert main(["receive", "--sqlite", str(source), "--capture", str(captured)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["l1_retained"] is True
    assert report["l2_updated"] is False
    assert retain_capture(source, captured)["receipt_id"] == report["receipt_id"]
    with closing(open_connection(source)) as conn:
        assert conn.execute("SELECT count(*) FROM tradingview_forecast_snapshots").fetchone() == (
            0,
        )
        assert conn.execute("SELECT count(*) FROM tradingview_source_receipts").fetchone() == (1,)
        saved = read_tool_results(conn, (report["receipt_id"],))[0]
        assert saved.tool_name == OFFICIAL_CONNECTOR_BATCH_TOOL
        assert saved.received_at_utc == RECEIVED
        assert saved.snapshot_date == SATURDAY
        assert saved.master_asof == DAY
        assert json.loads(saved.result_json)["structuredContent"] == body
        assert json.loads(saved.result_json)["_meta"] == {"native": True}
    handoff_path = tmp_path / "handoff.sqlite"
    selected = export_handoff(
        source, handoff_path, day=SATURDAY, since=RECEIVED - timedelta(seconds=1)
    )
    applied = import_handoff(handoff_path, canonical, selected)
    assert applied.report["rows_added"] == 2
    assert applied.report["remaining"] == 1
    assert applied.report["unresolved"] == 1
    with closing(open_connection(canonical)) as conn:
        rows = conn.execute(
            "SELECT ticker,fetch_status,eps_forecast_next_fy,quote_currency,"
            "estimate_currency,estimate_unit,target_period_label,fetched_at_utc "
            "FROM tradingview_forecast_snapshots ORDER BY ticker"
        ).fetchall()
        assert rows[0] == ("1000", "ok", -5, "JPY", None, None, None, RECEIVED.isoformat())
        assert rows[1][0:3] == ("1001", "unresolved", None)


@pytest.mark.parametrize("kind", ["tool_error", "malformed_business_payload"])
def test_failed_native_result_stays_l1_without_replacing_l2(tmp_path, kind):
    path = weekend_store(tmp_path / "market.sqlite")
    success = capture_file(tmp_path, identity="good-call")
    good = retain_capture(path, success)["receipt_id"]
    rebuild_from_receipts(path, SATURDAY, [good])
    with closing(open_connection(path)) as conn:
        before = conn.execute("SELECT * FROM tradingview_forecast_snapshots").fetchall()
    result = (
        CallToolResult(isError=True, content=[{"type": "text", "text": "HTTP 429"}])
        if kind == "tool_error"
        else CallToolResult(isError=False, content=[], structuredContent={"unexpected": None})
    )
    bad_file = capture_file(tmp_path, result=result, identity="failed-call")
    bad = retain_capture(path, bad_file)["receipt_id"]
    with pytest.raises(RebuildError):
        rebuild_from_receipts(path, SATURDAY, [bad])
    with closing(open_connection(path)) as conn:
        assert conn.execute("SELECT count(*) FROM tradingview_source_receipts").fetchone() == (2,)
        assert conn.execute("SELECT * FROM tradingview_forecast_snapshots").fetchall() == before


def test_receive_requires_existing_store_and_keeps_capture_timestamp(tmp_path):
    captured = capture_file(tmp_path)
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        retain_capture(missing, captured)
    assert not missing.exists()
    path = weekend_store(tmp_path / "market.sqlite")
    receipt_id = retain_capture(path, captured)["receipt_id"]
    with closing(open_connection(path)) as conn:
        assert read_tool_results(conn, (receipt_id,))[0].received_at_utc == RECEIVED


def test_other_tool_result_is_not_relabelled_as_a_batch(tmp_path):
    path = weekend_store(tmp_path / "market.sqlite")
    captured = capture_file(tmp_path, tool_name="mcp_tv_get_forecasts")
    receipt_id = retain_capture(path, captured)["receipt_id"]
    with pytest.raises(SourceDataError, match="unsupported tool"):
        rebuild_from_receipts(path, SATURDAY, [receipt_id])
    with closing(open_connection(path)) as conn:
        assert read_tool_results(conn, (receipt_id,))[0].tool_name == "mcp_tv_get_forecasts"
        assert conn.execute("SELECT count(*) FROM tradingview_forecast_snapshots").fetchone() == (
            0,
        )
