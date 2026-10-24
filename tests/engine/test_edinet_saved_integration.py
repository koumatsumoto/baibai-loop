"""Available saved EDINET inputs never pretend acquisition completed today."""

from contextlib import closing
from datetime import timedelta
from unittest.mock import patch

import pytest
from tests.engine.test_edinet_metric_eligibility import BASE, TARGET, _seed
from tests.engine.test_edinet_research_facts import source_zip
from tests.engine.test_screening_cli import _edinet_csv_zip
from tests.helpers.edinet_documents import store_edinet_documents

from baibai_engine.collection.edinet.archives import retain_source_archive
from baibai_engine.market.edinet_facts.store import record_initialized
from baibai_engine.market.edinet_integration import extract_saved_edinet
from baibai_engine.market.edinet_metrics.store import EDINETMetricBaselineError, read_edinet_metrics
from baibai_engine.market.edinet_models import EDINETProviderError
from baibai_engine.storage.observations.schema import open_connection


def test_missing_current_list_keeps_saved_snapshot_and_failed_acquisition_unchanged(tmp_path):
    path = tmp_path / "market.sqlite"
    _seed(path)
    before = path.read_bytes()
    with patch("requests.Session", side_effect=AssertionError("integration attempted acquisition")):
        report = extract_saved_edinet(path, asof=TARGET, lookback_days=0)
    assert report["edinet-metrics"]["exit_code"] == 0
    assert report["edinet-metrics"]["status"] == "unchanged"
    assert report["edinet-metrics"]["input_asof"] == BASE.isoformat()
    assert report["edinet-metrics"]["snapshot_asof"] == BASE.isoformat()
    assert report["edinet-metrics"]["deferred_inputs"] == ["latest_document_list_delayed"]
    assert report["edinet-facts"]["status"] == "skipped_not_initialized"
    assert path.read_bytes() == before
    assert read_edinet_metrics(path, TARGET) is None


def test_no_saved_window_is_deferred_without_writing_extraction_failure(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)):
        pass
    before = path.read_bytes()
    report = extract_saved_edinet(path, asof=TARGET)
    assert report["edinet-metrics"]["status"] == "deferred_input"
    assert report["edinet-metrics"]["deferred_inputs"] == ["document_list_unavailable"]
    assert path.read_bytes() == before


def test_incomplete_historical_window_is_not_shrunk_or_marked_extracted(tmp_path):
    path = tmp_path / "market.sqlite"
    _seed(path)
    before = path.read_bytes()
    report = extract_saved_edinet(path, asof=TARGET)
    assert "document_window_incomplete" in report["edinet-metrics"]["deferred_inputs"]
    assert report["edinet-metrics"]["snapshot_asof"] == BASE.isoformat()
    assert path.read_bytes() == before


def _next_listing(path):
    documents = _seed(path)
    for offset in (1, 2):
        store_edinet_documents(path, BASE + timedelta(days=offset), [])
    updated = dict(documents[0])
    updated.update(docID="S100NEXT", submitDateTime="2026-04-27 12:00")
    store_edinet_documents(path, TARGET, [updated])


def test_new_list_waits_for_csv_then_extracts_its_actual_source_day(tmp_path):
    path = tmp_path / "market.sqlite"
    _next_listing(path)
    before = path.read_bytes()
    deferred = extract_saved_edinet(path, asof=TARGET, lookback_days=3)
    assert deferred["edinet-metrics"]["deferred_inputs"] == ["csv_archives_unavailable"]
    assert path.read_bytes() == before
    with closing(open_connection(path)) as conn:
        retain_source_archive(conn, "S100NEXT", 5, _edinet_csv_zip(), stored_on=TARGET)
    result = extract_saved_edinet(path, asof=TARGET + timedelta(days=1), lookback_days=3)
    assert result["edinet-metrics"]["exit_code"] == 0
    assert result["edinet-metrics"]["snapshot_asof"] == TARGET.isoformat()
    records = read_edinet_metrics(path, TARGET)
    assert records["7203"].source_doc_id == "S100NEXT"
    assert records["8306"].cash == 123.0
    assert read_edinet_metrics(path, TARGET + timedelta(days=1)) is None


@pytest.mark.parametrize("corruption", ["baseline", "documents"])
def test_corrupt_saved_evidence_is_fatal_even_when_today_is_unavailable(tmp_path, corruption):
    path = tmp_path / "market.sqlite"
    _seed(path)
    with closing(open_connection(path)) as conn, conn:
        if corruption == "baseline":
            conn.execute("UPDATE edinet_metrics SET failure_reasons = 'not-json'")
        else:
            conn.execute("UPDATE edinet_document_lists SET response_json = 'not-json'")
    with pytest.raises((EDINETMetricBaselineError, EDINETProviderError)):
        extract_saved_edinet(path, asof=TARGET)


def test_present_but_invalid_csv_remains_extraction_failure(tmp_path):
    path = tmp_path / "market.sqlite"
    _next_listing(path)
    with closing(open_connection(path)) as conn:
        retain_source_archive(conn, "S100NEXT", 5, b"broken ZIP", stored_on=TARGET)
    result = extract_saved_edinet(path, asof=TARGET, lookback_days=3)
    assert result["edinet-metrics"]["exit_code"] == 1
    assert result["edinet-metrics"]["status"] == "failed"
    assert result["edinet-metrics"]["deferred_inputs"] == []


def test_facts_extract_known_ready_document_despite_failed_later_list(tmp_path):
    path = tmp_path / "market.sqlite"
    documents = _seed(path)
    record_initialized(path, BASE)
    with closing(open_connection(path)) as conn:
        retain_source_archive(conn, str(documents[0]["docID"]), 1, source_zip(), stored_on=BASE)
    result = extract_saved_edinet(path, asof=TARGET, lookback_days=0)
    facts = result["edinet-facts"]
    assert facts["exit_code"] == 0
    assert facts["counts"]["extracted"] == 1
    assert facts["missing_archive_count"] == 1
    assert "xbrl_archives_unavailable" in facts["deferred_inputs"]
    with closing(open_connection(path)) as conn:
        assert (
            conn.execute(
                "SELECT status FROM source_coverage WHERE source = 'edinet_documents' "
                "AND coverage_key = ?",
                (TARGET.isoformat(),),
            ).fetchone()[0]
            == "failed"
        )
        assert (
            conn.execute(
                "SELECT coverage_start FROM source_coverage "
                "WHERE source = 'edinet_research_facts' AND coverage_key = 'S100SAVED'"
            ).fetchone()[0]
            == BASE.isoformat()
        )


def test_unexpected_extractor_error_is_not_classified_as_deferred_input(tmp_path, monkeypatch):
    path = tmp_path / "market.sqlite"
    _next_listing(path)
    with closing(open_connection(path)) as conn:
        retain_source_archive(conn, "S100NEXT", 5, _edinet_csv_zip(), stored_on=TARGET)

    def broken(**kwargs):
        raise RuntimeError("unexpected extractor error")

    monkeypatch.setattr(
        "baibai_engine.market.edinet_integration.extract_edinet_metrics_from_plan", broken
    )
    with pytest.raises(RuntimeError, match="unexpected extractor error"):
        extract_saved_edinet(path, asof=TARGET, lookback_days=3)
