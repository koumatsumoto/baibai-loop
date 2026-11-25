from contextlib import closing
from dataclasses import replace
from datetime import timedelta

import pytest
from mcp.types import CallToolResult, TextContent
from tests.helpers.tradingview import (
    DAY,
    NOW,
    _sdk,
    native_inputs,
    payload,
    retain_result,
    store,
    stored,
)

from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.tradingview_handoff import (
    TradingViewHandoff,
    export_handoff,
    import_handoff,
)
from baibai_jobs.maintenance.tradingview_rebuild import rebuild_from_receipts
from baibai_jobs.observability.tradingview import (
    SourceDataError,
)


def test_late_partial_handoff_adds_missing_rows_without_narrowing_saved_selection(tmp_path):
    source, canonical = (
        store(tmp_path / "source.sqlite", 3),
        store(tmp_path / "canonical.sqlite", 3),
    )
    first = retain_result(canonical, _sdk(payload(["TSE:1000"])))
    rebuild_from_receipts(canonical, DAY, [first])
    before = stored(canonical)[0]
    retain_result(source, _sdk(payload(["TSE:1002"])), symbols=("TSE:1002",))
    with closing(open_connection(canonical)) as conn, conn:
        conn.execute(
            "INSERT INTO jpx_regulation_sources VALUES (?, ?, ?)",
            (DAY.isoformat(), "fresh-jpx", NOW.isoformat()),
        )
    stage = tmp_path / "stage.sqlite"
    remote, master, calendar = native_inputs(source, tmp_path, day=DAY)
    selected = export_handoff(
        source,
        stage,
        day=DAY,
        master_input=master,
        calendar_input=calendar,
        since=NOW - timedelta(seconds=1),
    )
    result = import_handoff(stage, canonical, selected, store=remote)
    assert result.report["rows_added"] == 1
    assert result.report["rows"] == 2
    assert result.complete is True
    assert result.report["remaining"] == 1
    assert stored(canonical)[0] == before
    assert [row[1] for row in stored(canonical)] == ["1000", "1002"]
    assert import_handoff(stage, canonical, selected, store=remote).report["rows_added"] == 0
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
    with pytest.raises(ValueError, match="non-native master"):
        TradingViewHandoff.from_json({**selected.to_json(), "master_request_ids": ["other"]})


@pytest.mark.parametrize("has_prior_receipt", [True, False])
def test_a_canonical_first_row_is_retained_even_when_a_handoff_repeats_its_ticker(
    tmp_path, has_prior_receipt
):
    source, canonical = (
        store(tmp_path / "source.sqlite", 1),
        store(tmp_path / "canonical.sqlite", 1),
    )
    first = retain_result(canonical, _sdk(payload(["TSE:1000"])))
    rebuild_from_receipts(canonical, DAY, [first])
    before = stored(canonical)
    if not has_prior_receipt:
        with closing(open_connection(canonical)) as conn, conn:
            conn.execute("DELETE FROM tradingview_source_receipts WHERE receipt_id=?", (first,))
    changed = payload(["TSE:1000"])
    changed["data"]["TSE:1000"]["close"] = 999
    received = retain_result(source, _sdk(changed), received=NOW + timedelta(minutes=1))
    stage = tmp_path / "stage.sqlite"
    remote, master, calendar = native_inputs(source, tmp_path, day=DAY)
    selected = export_handoff(
        source, stage, day=DAY, master_input=master, calendar_input=calendar, since=NOW
    )
    result = import_handoff(stage, canonical, selected, store=remote)
    assert result.report["rows_added"] == 0
    assert result.report["raw_receipts_added"] == 1
    assert stored(canonical) == before
    with closing(open_connection(canonical)) as conn:
        assert {
            row[0] for row in conn.execute("SELECT receipt_id FROM tradingview_source_receipts")
        } == ({first, received} if has_prior_receipt else {received})


def test_new_canonical_universe_refuses_the_old_handoff_before_receiving_rows(tmp_path):
    source, canonical = (
        store(tmp_path / "source.sqlite", 1),
        store(tmp_path / "canonical.sqlite", 2),
    )
    retain_result(source, _sdk(payload(["TSE:1000"])))
    stage = tmp_path / "stage.sqlite"
    remote, master, calendar = native_inputs(source, tmp_path, day=DAY)
    selected = export_handoff(
        source, stage, day=DAY, master_input=master, calendar_input=calendar, since=NOW
    )
    with pytest.raises(SourceDataError, match="universe"):
        import_handoff(stage, canonical, selected, store=remote)
    with closing(open_connection(canonical)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tradingview_source_receipts").fetchone()[0] == 0


def test_declared_receipt_must_exist_in_packet_even_if_canonical_has_it(tmp_path):
    source = store(tmp_path / "source.sqlite", 2)
    canonical = store(tmp_path / "canonical.sqlite", 2)
    prior = retain_result(canonical, _sdk(payload(["TSE:1000"])))
    received = retain_result(source, _sdk(payload(["TSE:1001"])), symbols=("TSE:1001",))
    stage = tmp_path / "stage.sqlite"
    remote, master, calendar = native_inputs(source, tmp_path, day=DAY)
    handoff = export_handoff(
        source, stage, day=DAY, master_input=master, calendar_input=calendar, since=NOW
    )
    assert handoff.receipt_ids == (received,)
    with pytest.raises(SourceDataError, match="missing from its packet"):
        import_handoff(
            stage, canonical, replace(handoff, receipt_ids=(received, prior)), store=remote
        )
    with closing(open_connection(canonical)) as conn:
        assert conn.execute("SELECT receipt_id FROM tradingview_source_receipts").fetchall() == [
            (prior,)
        ]
    assert stored(canonical) == []


@pytest.mark.parametrize("failure", ["provider_rate_limit", "invalid_number", "missing_field"])
def test_rejected_response_is_received_without_creating_a_normal_snapshot(tmp_path, failure):
    source, canonical = (
        store(tmp_path / "source.sqlite", 2),
        store(tmp_path / "canonical.sqlite", 2),
    )
    symbols = ["TSE:1000", "TSE:1001"]
    reason = "provider_response"
    if failure == "provider_rate_limit":
        result = CallToolResult(isError=True, content=[TextContent(type="text", text="HTTP 429")])
        reason = failure
    else:
        response = payload(symbols)
        if failure == "invalid_number":
            response["data"][symbols[1]]["close"] = "private-invalid-number"
        else:
            del response["data"][symbols[1]]["AnalystRating"]
        result = _sdk(response)
    receipt_id = retain_result(source, result, symbols=tuple(symbols))
    stage = tmp_path / "stage.sqlite"
    remote, master, calendar = native_inputs(source, tmp_path, day=DAY)
    selected = export_handoff(
        source, stage, day=DAY, master_input=master, calendar_input=calendar, since=NOW
    )
    result = import_handoff(stage, canonical, selected, store=remote)
    assert result.complete is False
    assert result.report["raw_receipts_added"] == 1
    assert result.report["rows_added"] == 0
    assert result.report["rejected_receipts"] == [{"receipt_id": receipt_id, "reason": reason}]
    assert stored(canonical) == []
    assert (
        import_handoff(stage, canonical, selected, store=remote).report["raw_receipts_added"] == 0
    )


def test_overnight_receipt_handoff_keeps_actual_time_and_is_idempotent(tmp_path):
    source = store(tmp_path / "source.sqlite", 1)
    canonical = store(tmp_path / "canonical.sqlite", 1)
    received = NOW.replace(hour=16, minute=31)
    retain_result(source, _sdk(payload(["TSE:1000"])), received=received)
    stage = tmp_path / "stage.sqlite"
    remote, master, calendar = native_inputs(source, tmp_path, day=DAY)
    handoff = export_handoff(
        source, stage, day=DAY, master_input=master, calendar_input=calendar, since=NOW
    )
    first = import_handoff(stage, canonical, handoff, store=remote)
    assert first.report["rows_added"] == 1
    assert first.complete
    (row,) = stored(canonical)
    assert row[0] == DAY.isoformat()
    assert row[3] == received.isoformat()
    assert import_handoff(stage, canonical, handoff, store=remote).report["rows_added"] == 0
