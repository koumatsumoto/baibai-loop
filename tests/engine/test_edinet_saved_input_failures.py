"""Input delays cannot conceal corrupt receipts or other ready issuers' updates."""

import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from tests.engine.test_edinet_metric_eligibility import BASE, TARGET, _seed
from tests.engine.test_edinet_saved_integration import _next_listing
from tests.engine.test_source_maintenance_cli import _edinet_csv_zip
from tests.helpers.edinet_documents import store_edinet_documents

from baibai_engine.data_collection.edinet.archives import retain_source_archive
from baibai_engine.data_collection.edinet.receipts import retain_list_response
from baibai_engine.market.edinet_integration import extract_saved_edinet
from baibai_engine.machine_analysis.observations.disclosures.edinet_list_rebuild import rebuild_edinet_document_day
from baibai_engine.market.edinet_metrics.csv import parse_csv_zip_metric_record
from baibai_engine.market.edinet_metrics.revision import DEFERRED_CSV_REASON
from baibai_engine.market.edinet_metrics.service import prepare_edinet_metric_plan
from baibai_engine.market.edinet_metrics.store import (
    EDINETMetricBaselineError,
    read_edinet_metric_baseline,
    read_edinet_metrics,
)
from baibai_engine.market.edinet_reader import EDINETReader
from baibai_engine.storage.observations.schema import open_connection


def _mixed_inputs(path, content):
    _next_listing(path)
    ready = dict(EDINETReader(path).list_documents(BASE)[1])
    ready.update(docID="S100READY", submitDateTime="2026-04-27 12:00")
    waiting = EDINETReader(path).list_documents(TARGET)[0]
    store_edinet_documents(path, TARGET, [waiting, ready])
    with closing(open_connection(path)) as conn:
        retain_source_archive(conn, "S100READY", 5, content, stored_on=TARGET)


def test_missing_csv_does_not_hide_another_selected_corrupt_zip(tmp_path):
    path = tmp_path / "market.sqlite"
    _mixed_inputs(path, b"corrupt ZIP")
    result = extract_saved_edinet(path, asof=TARGET, lookback_days=3)
    metrics = result["edinet-metrics"]
    assert metrics["missing_archive_count"] == 1
    assert metrics["deferred_inputs"] == ["csv_archives_unavailable"]
    assert metrics["status"] == "failed"
    assert metrics["exit_code"] == 1
    assert read_edinet_metric_baseline(path, TARGET).asof_date == BASE


def test_partial_inputs_update_ready_issuer_and_retry_missing_archive_after_arrival(tmp_path):
    path = tmp_path / "market.sqlite"
    _mixed_inputs(path, _edinet_csv_zip())
    result = extract_saved_edinet(path, asof=TARGET + timedelta(days=1), lookback_days=3)
    assert result["edinet-metrics"]["status"] == "partial"
    assert result["edinet-metrics"]["exit_code"] == 0
    baseline = read_edinet_metric_baseline(path, TARGET)
    assert baseline.asof_date == TARGET
    assert baseline.rows["7203"].record.failure_reasons == (DEFERRED_CSV_REASON,)
    assert baseline.rows["7203"].record.cash is None
    selected = EDINETReader(path).load_metric_records(TARGET)
    assert set(selected) == {"8306"}
    assert selected["8306"].source_doc_id == "S100READY"
    assert selected["8306"].sales_ttm is not None
    with closing(open_connection(path)) as conn:
        assert (
            conn.execute(
                "SELECT status FROM source_coverage WHERE source = 'edinet_metrics' "
                "AND coverage_key = ?",
                (TARGET.isoformat(),),
            ).fetchone()[0]
            == "partial"
        )
    assert read_edinet_metrics(path, TARGET) is None
    before = path.read_bytes()
    extract_saved_edinet(path, asof=TARGET + timedelta(days=1), lookback_days=3)
    assert path.read_bytes() == before
    with closing(open_connection(path)) as conn:
        retain_source_archive(conn, "S100NEXT", 5, _edinet_csv_zip(), stored_on=TARGET)
    with patch(
        "baibai_engine.market.edinet_metrics.service.parse_csv_zip_metric_record",
        wraps=parse_csv_zip_metric_record,
    ) as parse:
        recovered = extract_saved_edinet(path, asof=TARGET + timedelta(days=1), lookback_days=3)
    assert recovered["edinet-metrics"]["status"] == "completed"
    assert [call.kwargs["doc_id"] for call in parse.call_args_list] == ["S100NEXT"]
    assert set(EDINETReader(path).load_metric_records(TARGET)) == {"7203", "8306"}
    assert read_edinet_metrics(path, TARGET) is not None
    assert read_edinet_metrics(path, TARGET + timedelta(days=1)) is None


@pytest.mark.parametrize("damage", ["doc_ids", "numeric_value", "claimed_complete"])
def test_partial_baseline_is_validated_without_falling_back_past_damage(tmp_path, damage):
    path = tmp_path / "market.sqlite"
    _mixed_inputs(path, _edinet_csv_zip())
    extract_saved_edinet(path, asof=TARGET, lookback_days=3)
    with closing(open_connection(path)) as conn, conn:
        if damage == "doc_ids":
            conn.execute(
                "UPDATE source_coverage SET error = ? WHERE source = 'edinet_metrics' "
                "AND coverage_key = ?",
                (
                    json.dumps({"reason": "csv_archives_unavailable", "doc_ids": ["S100OTHER"]}),
                    TARGET.isoformat(),
                ),
            )
        elif damage == "numeric_value":
            conn.execute(
                "UPDATE edinet_metrics SET cash = 999 WHERE asof_date = ? AND ticker = '7203'",
                (TARGET.isoformat(),),
            )
        else:
            conn.execute(
                "UPDATE source_coverage SET status = 'ok', error = NULL "
                "WHERE source = 'edinet_metrics' AND coverage_key = ?",
                (TARGET.isoformat(),),
            )
    with pytest.raises(EDINETMetricBaselineError):
        read_edinet_metric_baseline(path, TARGET)


def test_old_outside_window_edit_cannot_quarantine_current_annual(tmp_path):
    path = tmp_path / "market.sqlite"
    documents = _seed(path)
    for offset in (1, 2, 3):
        store_edinet_documents(path, BASE - timedelta(days=offset), [])
    old = dict(documents[0])
    old.update(
        docID="S100OLD",
        periodStart="2023-04-01",
        periodEnd="2024-03-31",
        submitDateTime="2024-04-01 12:00",
    )
    store_edinet_documents(path, date(2024, 4, 1), [old])
    edit = dict(old)
    edit.update(docInfoEditStatus="1", docDescription="old edit", opeDateTime="2024-04-02 12:00")
    store_edinet_documents(path, date(2024, 4, 2), [edit])
    before = path.read_bytes()
    legacy = prepare_edinet_metric_plan(
        asof_date=BASE, lookback_days=3, reader=EDINETReader(path), sqlite_path=path
    )
    assert legacy is not None
    assert legacy.events_by_ticker == {}
    result = extract_saved_edinet(path, asof=BASE, lookback_days=3)
    assert result["edinet-metrics"]["status"] == "unchanged"
    assert EDINETReader(path).load_metric_records(BASE)["7203"].cash == 123.0
    assert path.read_bytes() == before


def _receipt(path, content, seconds):
    with closing(open_connection(path)) as conn:
        retain_list_response(
            conn,
            doc_date=TARGET,
            received_at=datetime(2026, 4, 27, tzinfo=UTC) + timedelta(seconds=seconds),
            body=content,
        )
    return rebuild_edinet_document_day(path, TARGET)


@pytest.mark.parametrize(
    "bad", [b"{broken JSON", b'{"metadata":{"status":"500","resultset":{"count":0}},"results":[]}']
)
def test_retained_projection_failure_remains_error_without_a_fresh_handoff(tmp_path, bad):
    path = tmp_path / "market.sqlite"
    _seed(path)
    good = b'{"metadata":{"resultset":{"count":0}},"results":[]}'
    assert _receipt(path, good, 0).error is None
    assert _receipt(path, bad, 1).error is not None
    before = path.read_bytes()
    report = extract_saved_edinet(path, asof=TARGET, lookback_days=0)
    assert report["edinet-list"]["exit_code"] == 1
    assert report["edinet-list"]["failed_days"] == [TARGET.isoformat()]
    assert path.read_bytes() == before
    assert _receipt(path, good, 2).error is None
    recovered = extract_saved_edinet(path, asof=TARGET, lookback_days=0)
    assert "edinet-list" not in recovered


def test_failed_acquisition_with_valid_retained_receipt_is_not_a_projection_error(tmp_path):
    path = tmp_path / "market.sqlite"
    _seed(path)
    good = b'{"metadata":{"resultset":{"count":0}},"results":[]}'
    _receipt(path, good, 0)
    with closing(open_connection(path)) as conn, conn:
        conn.execute(
            "UPDATE source_coverage SET status = 'failed', error = 'HTTP 302' "
            "WHERE source = 'edinet_documents' AND coverage_key = ?",
            (TARGET.isoformat(),),
        )
    before = path.read_bytes()
    result = extract_saved_edinet(path, asof=TARGET, lookback_days=0)
    assert "edinet-list" not in result
    assert result["edinet-metrics"]["deferred_inputs"] == ["latest_document_list_delayed"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("event", ["correction", "withdrawal"])
def test_known_event_inside_saved_window_keeps_its_effect(tmp_path, event):
    path = tmp_path / "market.sqlite"
    _next_listing(path)
    if event == "correction":
        changed = EDINETReader(path).list_documents(TARGET)[0]
        changed.update(docTypeCode="130", parentDocID="S100SAVED")
        with closing(open_connection(path)) as conn:
            retain_source_archive(conn, "S100NEXT", 5, _edinet_csv_zip(), stored_on=TARGET)
    else:
        changed = dict(EDINETReader(path).list_documents(BASE)[0])
        changed.update(
            docID="S100WITHDRAW",
            parentDocID="S100SAVED",
            withdrawalStatus="1",
            opeDateTime="2026-04-27 12:00",
        )
    store_edinet_documents(path, TARGET, [changed])
    report = extract_saved_edinet(path, asof=TARGET, lookback_days=3)
    assert report["edinet-metrics"]["status"] == "completed"
    assert read_edinet_metric_baseline(path, TARGET).asof_date == TARGET
    selected = EDINETReader(path).load_metric_records(TARGET)
    if event == "correction":
        assert selected["7203"].source_doc_id == "S100NEXT"
    else:
        assert "7203" not in selected
    assert selected["8306"].cash == 123.0
