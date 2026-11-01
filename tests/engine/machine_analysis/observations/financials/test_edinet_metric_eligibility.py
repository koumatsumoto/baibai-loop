"""Independent EDINET acquisition does not define eligibility of saved metrics."""

from __future__ import annotations

from contextlib import closing
from datetime import date
from unittest.mock import patch

import pytest
from tests.helpers.edinet_documents import store_edinet_documents
from tests.helpers.edinet_saved_inputs import BASE, RECORDED, TARGET, _seed, _write_snapshot

from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.sqlite_coverage.edinet import (
    _append_edinet_metrics_coverage_issues,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.revision import (
    EDINET_METRIC_REVISION,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.store import (
    EDINETMetricBaselineError,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.write import (
    store_edinet_metrics,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.machine_analysis.observations.financials.edinet_reader import EDINETReader
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def _issues(path):
    issues = []
    _append_edinet_metrics_coverage_issues(path, issues, asof_date=TARGET)
    return issues


def test_delayed_acquisition_preserves_eligible_saved_values_and_provenance(tmp_path):
    path = tmp_path / "market.sqlite"
    _seed(path)
    before = path.read_bytes()
    with patch("requests.Session", side_effect=AssertionError("reader attempted HTTP")):
        records = EDINETReader(path).load_metric_records(TARGET)
    assert set(records) == {"7203", "8306"}
    assert records["7203"].cash == 123.0
    assert records["7203"].snapshot_asof == BASE
    assert records["7203"].snapshot_recorded_at_utc == RECORDED
    assert records["7203"].source_submit_datetime == "2026-04-24 12:00"
    assert records["7203"].source_period_end == date(2026, 3, 31)
    assert _issues(path) == []
    assert path.read_bytes() == before
    with closing(open_connection(path)) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM edinet_metrics WHERE asof_date = ?", (TARGET.isoformat(),)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT status FROM source_coverage WHERE source = 'edinet_metrics' "
                "AND coverage_key = ?",
                (TARGET.isoformat(),),
            ).fetchone()[0]
            == "failed"
        )


@pytest.mark.parametrize("change", ["correction", "withdrawal", "edit", "revision"])
def test_known_changes_invalidate_only_affected_saved_values(tmp_path, change):
    path = tmp_path / "market.sqlite"
    documents = _seed(path)
    if change == "revision":
        with closing(open_connection(path)) as conn, conn:
            conn.execute(
                "UPDATE edinet_metrics SET extractor_revision = 'old' WHERE ticker = '7203'"
            )
    else:
        document = dict(documents[0])
        if change == "correction":
            document.update(
                docID="S100CORRECT",
                docTypeCode="130",
                parentDocID="S100SAVED",
                submitDateTime="2026-04-27 12:00",
            )
        elif change == "withdrawal":
            document.update(withdrawalStatus="1", opeDateTime="2026-04-27 12:00")
        else:
            document.update(
                docInfoEditStatus="1", docDescription="edited", opeDateTime="2026-04-27 12:00"
            )
        store_edinet_documents(path, TARGET, [document])
    assert set(EDINETReader(path).load_metric_records(TARGET)) == {"8306"}
    assert _issues(path) == []


def test_unversioned_saved_row_is_not_adopted(tmp_path):
    path = tmp_path / "market.sqlite"
    _seed(path)
    with closing(open_connection(path)) as conn, conn:
        conn.execute(
            "UPDATE edinet_metrics SET extractor_revision = NULL, source_document_revision = NULL "
            "WHERE ticker = '7203'"
        )
    assert set(EDINETReader(path).load_metric_records(TARGET)) == {"8306"}
    assert _issues(path) == []


def test_inconsistent_newest_success_never_falls_back_to_older_baseline(tmp_path):
    path = tmp_path / "market.sqlite"
    _seed(path)
    store_edinet_metrics(
        path,
        TARGET,
        [{"ticker": "7203", "cash": 999.0, "extractor_revision": EDINET_METRIC_REVISION}],
    )
    with closing(open_connection(path)) as conn, conn:
        conn.execute(
            "UPDATE edinet_metrics SET failure_reasons = 'not-json' WHERE asof_date = ?",
            (TARGET.isoformat(),),
        )
    with pytest.raises(EDINETMetricBaselineError):
        EDINETReader(path).load_metric_records(TARGET)
    assert _issues(path)[0].source == "edinet_metrics"


def test_corrupt_retained_document_evidence_is_not_optional_missing_input(tmp_path):
    path = tmp_path / "market.sqlite"
    _seed(path)
    with closing(open_connection(path)) as conn, conn:
        conn.execute("UPDATE edinet_document_lists SET response_json = 'not-json'")
    with pytest.raises(EDINETProviderError, match="saved response is invalid"):
        EDINETReader(path).load_metric_records(TARGET)
    assert "saved response is invalid" in _issues(path)[0].reason


@pytest.mark.parametrize("state", ["saved", "missing", "incompatible"])
def test_actual_coverage_and_reader_agree_on_optional_eligibility(state):
    from tests.helpers.legacy_coverage import (
        _complete_coverage_database,
        _verify_screening_sqlite_coverage,
    )

    asof = date(2026, 5, 8)
    with _complete_coverage_database() as path:
        with closing(open_connection(path)) as conn, conn:
            conn.execute("DELETE FROM edinet_metrics")
            conn.execute("DELETE FROM source_coverage WHERE source = 'edinet_metrics'")
        if state != "missing":
            _seed(path)
            if state == "incompatible":
                with closing(open_connection(path)) as conn, conn:
                    conn.execute("UPDATE edinet_metrics SET extractor_revision = 'old'")
        assert _verify_screening_sqlite_coverage(path, asof) == ()
        records = EDINETReader(path).load_metric_records(asof)
        assert set(records) == ({"7203", "8306"} if state == "saved" else set())


def test_late_successful_next_snapshot_is_adopted_on_its_actual_date(tmp_path):
    path = tmp_path / "market.sqlite"
    documents = _seed(path)
    before = EDINETReader(path).load_metric_records(TARGET)
    assert before["7203"].snapshot_asof == BASE
    correction = dict(documents[0])
    correction.update(
        docID="S100CORRECT",
        docTypeCode="130",
        parentDocID="S100SAVED",
        submitDateTime="2026-04-27 12:00",
    )
    store_edinet_documents(path, TARGET, [correction])
    # Known correction cannot be filled with the prior document while parsing is delayed.
    assert "7203" not in EDINETReader(path).load_metric_records(TARGET)
    _write_snapshot(path, TARGET, cash=456.0)
    after = EDINETReader(path).load_metric_records(TARGET)
    assert after["7203"].cash == 456.0
    assert after["7203"].source_doc_id == "S100CORRECT"
    assert after["7203"].snapshot_asof == TARGET
    assert after["7203"].source_period_end == date(2026, 3, 31)
    assert EDINETReader(path).load_metric_records(BASE)["7203"].cash == 123.0
    assert _issues(path) == []
