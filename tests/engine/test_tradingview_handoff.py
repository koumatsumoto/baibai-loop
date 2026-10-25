from contextlib import closing
from datetime import timedelta

import pytest
from mcp.types import CallToolResult, TextContent
from tests.engine.test_jquants_projection import body, receive
from tests.engine.test_tradingview_collector import DAY, NOW, _sdk, payload, store, stored
from tests.engine.test_tradingview_rebuild import _retain

from baibai_engine.machine_analysis.external_assessment.tradingview.diagnostics import SourceDataError
from baibai_engine.data_collection.tradingview.handoff import (
    TradingViewHandoff,
    export_handoff,
    import_handoff,
)
from baibai_engine.data_collection.tradingview.rebuild import rebuild_from_receipts
from baibai_engine.storage.observations.schema import open_connection


def test_late_partial_handoff_adds_missing_rows_without_narrowing_saved_selection(tmp_path):
    source, canonical = (
        store(tmp_path / "source.sqlite", 3),
        store(tmp_path / "canonical.sqlite", 3),
    )
    first = _retain(canonical, _sdk(payload(["TSE:1000"])))
    rebuild_from_receipts(canonical, DAY, [first])
    before = stored(canonical)[0]
    _retain(source, _sdk(payload(["TSE:1002"])), symbols=("TSE:1002",))
    with closing(open_connection(canonical)) as conn, conn:
        conn.execute(
            "INSERT INTO jpx_regulation_sources VALUES (?, ?, ?)",
            (DAY.isoformat(), "fresh-jpx", NOW.isoformat()),
        )
    stage = tmp_path / "stage.sqlite"
    selected = export_handoff(source, stage, day=DAY, since=NOW - timedelta(seconds=1))
    result = import_handoff(stage, canonical, selected)
    assert result["rows_added"] == 1
    assert result["rows"] == 2
    assert result["remaining"] == 1
    assert stored(canonical)[0] == before
    assert [row[1] for row in stored(canonical)] == ["1000", "1002"]
    assert import_handoff(stage, canonical, selected)["rows_added"] == 0
    with closing(open_connection(canonical)) as conn:
        assert (
            conn.execute("SELECT source_name FROM jpx_regulation_sources").fetchone()[0]
            == "fresh-jpx"
        )
    with closing(open_connection(stage)) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM tradingview_forecast_snapshots").fetchone()[0] == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM jpx_regulation_sources").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM jquants_master_snapshots").fetchone()[0] == 0
    assert TradingViewHandoff.from_json(selected.to_json()) == selected


def test_a_canonical_first_row_is_retained_even_when_a_handoff_repeats_its_ticker(tmp_path):
    source, canonical = (
        store(tmp_path / "source.sqlite", 1),
        store(tmp_path / "canonical.sqlite", 1),
    )
    first = _retain(canonical, _sdk(payload(["TSE:1000"])))
    rebuild_from_receipts(canonical, DAY, [first])
    before = stored(canonical)
    changed = payload(["TSE:1000"])
    changed["data"]["TSE:1000"]["close"] = 999
    _retain(source, _sdk(changed), received=NOW + timedelta(minutes=1))
    stage = tmp_path / "stage.sqlite"
    selected = export_handoff(source, stage, day=DAY, since=NOW)
    result = import_handoff(stage, canonical, selected)
    assert result["rows_added"] == 0
    assert result["raw_receipts_added"] == 1
    assert stored(canonical) == before


def test_new_canonical_universe_refuses_the_old_handoff_before_receiving_rows(tmp_path):
    source, canonical = (
        store(tmp_path / "source.sqlite", 1),
        store(tmp_path / "canonical.sqlite", 2),
    )
    _retain(source, _sdk(payload(["TSE:1000"])))
    stage = tmp_path / "stage.sqlite"
    selected = export_handoff(source, stage, day=DAY, since=NOW)
    with pytest.raises(SourceDataError, match="universe"):
        import_handoff(stage, canonical, selected)
    with closing(open_connection(canonical)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tradingview_source_receipts").fetchone()[0] == 0


def test_rejected_response_is_received_without_creating_a_normal_snapshot(tmp_path):
    source, canonical = (
        store(tmp_path / "source.sqlite", 1),
        store(tmp_path / "canonical.sqlite", 1),
    )
    _retain(
        source, CallToolResult(isError=True, content=[TextContent(type="text", text="HTTP 429")])
    )
    stage = tmp_path / "stage.sqlite"
    selected = export_handoff(source, stage, day=DAY, since=NOW)
    result = import_handoff(stage, canonical, selected)
    assert result["raw_receipts_added"] == 1
    assert result["rows_added"] == 0
    assert result["rejected_receipts"] == [
        {"receipt_id": selected.receipt_ids[0], "reason": "provider_rate_limit"}
    ]
    assert stored(canonical) == []


def test_new_exact_date_master_is_replayed_from_its_original_pages(tmp_path):
    import json

    source, canonical = (
        store(tmp_path / "source.sqlite", 2500),
        store(tmp_path / "canonical.sqlite", 1),
    )
    with closing(open_connection(source)) as conn:
        records = [
            json.loads(row[0])
            for row in conn.execute("SELECT record_json FROM jquants_master_snapshots")
        ]
    receive(source, "master", {"date": DAY.isoformat()}, body(records), NOW - timedelta(seconds=1))
    with closing(open_connection(canonical)) as conn, conn:
        conn.execute("DELETE FROM jquants_master_snapshots")
    _retain(source, _sdk(payload(["TSE:1000"])))
    stage = tmp_path / "stage.sqlite"
    selected = export_handoff(source, stage, day=DAY, since=NOW - timedelta(seconds=2))
    assert len(selected.master_request_ids) == 1
    result = import_handoff(stage, canonical, selected)
    assert result["rows_added"] == 1
    assert result["expected_universe"] == 2500
