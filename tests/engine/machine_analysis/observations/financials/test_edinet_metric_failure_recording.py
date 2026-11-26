"""An unsuccessful extraction must not replace another attempt's committed metrics."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from threading import Event, current_thread

import pytest
from tests.helpers.edinet_metrics import FakeEDINETMetricReader, _edinet_csv_zip, _edinet_document

from baibai_engine.data_collection.providers.edinet.models import EDINETProviderError
from baibai_engine.machine_analysis.observations.financials.edinet_metrics import service
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.revision import (
    DEFERRED_CSV_REASON,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.store import (
    read_edinet_metric_baseline,
    read_edinet_metrics,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.write import (
    store_edinet_metrics,
)
from baibai_engine.storage.sqlite.source_cache.coverage import record_source_coverage
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

DAY = date(2026, 4, 24)
GOOD = "S100GOOD"
BAD = "S100FAIL"
DEFERRED = "S100WAIT"


class ListingFailure(FakeEDINETMetricReader):
    def list_documents(self, on_date: date) -> list[dict[str, object]]:
        raise EDINETProviderError("synthetic listing failure")


def _failing_extract(path: Path, kind: str) -> tuple[int, str]:
    output = StringIO()
    reader = (
        ListingFailure()
        if kind == "list_error"
        else FakeEDINETMetricReader(
            documents=[_edinet_document(ticker="96820", doc_id=BAD)] if kind == "csv_error" else [],
        )
    )
    code = service.extract_edinet_metrics_command(
        asof_date=DAY, lookback_days=0, reader=reader, sqlite_path=path, stdout=output
    )
    return code, output.getvalue()


def _publish(path: Path, *, partial: bool = False, day: date = DAY) -> None:
    rows = [_edinet_document(ticker="96820", doc_id=GOOD)]
    if partial:
        rows.append(_edinet_document(ticker="72030", doc_id=DEFERRED))
    reader = FakeEDINETMetricReader(documents=rows, zip_by_doc_id={GOOD: _edinet_csv_zip()})
    out = StringIO()
    plan = service.prepare_edinet_metric_plan(
        asof_date=day, lookback_days=0, reader=reader, sqlite_path=path, stdout=out
    )
    assert plan is not None
    assert (
        service.extract_edinet_metrics_from_plan(
            asof_date=day,
            plan=plan,
            reader=reader,
            sqlite_path=path,
            stdout=out,
            unavailable_doc_ids=frozenset({DEFERRED}) if partial else frozenset(),
        )
        == 0
    )


@pytest.mark.parametrize(
    ("failure_kind", "loser_read"),
    [("list_error", 1), ("no_candidates", 1), ("csv_error", 2)],
)
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("wal", [False, True])
def test_failed_attempt_cannot_replace_newly_committed_metrics(
    tmp_path, monkeypatch, failure_kind, loser_read, partial, wal
):
    path = tmp_path / "market.sqlite"
    if wal:
        with closing(open_connection(path)) as conn:
            assert conn.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    read_done, resume_failure = Event(), Event()
    failing_reads = 0

    def pause_after_read(sqlite_path, target_asof):
        nonlocal failing_reads
        result = read_edinet_metric_baseline(sqlite_path, target_asof)
        if current_thread().name.startswith("failed-edinet"):
            failing_reads += 1
            if failing_reads == loser_read:
                # Real read transaction has closed. No replacement of its result.
                assert result is None
                read_done.set()
                assert resume_failure.wait(timeout=5)
        return result

    monkeypatch.setattr(service, "read_edinet_metric_baseline", pause_after_read)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="failed-edinet") as pool:
        failure = pool.submit(_failing_extract, path, failure_kind)
        try:
            assert read_done.wait(timeout=5)
            _publish(path, partial=partial)
            winner = read_edinet_metric_baseline(path, DAY)
            assert winner is not None
            assert winner.coverage_status == ("partial" if partial else "ok")
            assert winner.rows["9682"].record.fcf_ttm == 700
            if partial:
                assert winner.rows["7203"].record.failure_reasons == (DEFERRED_CSV_REASON,)
            committed = path.read_bytes()
        finally:
            resume_failure.set()
        code, log = failure.result(timeout=5)

    assert code == 1
    if failure_kind == "csv_error":
        assert "output=not-written" in log
    assert failing_reads == loser_read
    assert read_edinet_metric_baseline(path, DAY) == winner
    assert path.read_bytes() == committed


def test_failed_attempt_does_not_erase_metrics_with_missing_coverage(tmp_path):
    path = tmp_path / "market.sqlite"
    _publish(path)
    with closing(open_connection(path)) as conn, conn:
        # Corrupt one local fixture only: coverage was invalidated, physical rows remain.
        conn.execute("DELETE FROM source_coverage WHERE source = 'edinet_metrics'")
    assert read_edinet_metric_baseline(path, DAY) is None
    orphaned = path.read_bytes()
    assert _failing_extract(path, "csv_error")[0] == 1
    assert path.read_bytes() == orphaned


def test_failed_attempt_does_not_replace_an_overlapping_unresolved_marker(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn, conn:
        record_source_coverage(
            conn,
            source="edinet_metrics",
            coverage_key=(DAY + timedelta(days=1)).isoformat(),
            coverage_start=DAY.isoformat(),
            coverage_end=(DAY + timedelta(days=1)).isoformat(),
            record_count=0,
            status="partial",
            error="synthetic inconsistent descriptor",
        )
    assert read_edinet_metric_baseline(path, DAY) is None
    orphaned = path.read_bytes()
    assert _failing_extract(path, "csv_error")[0] == 1
    assert path.read_bytes() == orphaned


def test_first_failure_still_records_empty_marker_and_success_can_recover(tmp_path):
    path = tmp_path / "market.sqlite"
    prev = DAY - timedelta(days=1)
    _publish(path, day=prev)
    earlier = read_edinet_metric_baseline(path, prev)
    assert earlier is not None
    assert _failing_extract(path, "csv_error")[0] == 1
    with closing(open_connection(path)) as conn:
        marker = conn.execute(
            "SELECT status, record_count FROM source_coverage "
            "WHERE source = 'edinet_metrics' AND coverage_key = ?",
            (DAY.isoformat(),),
        ).fetchone()
        assert marker == ("failed", 0)
    assert read_edinet_metric_baseline(path, DAY) == earlier
    assert read_edinet_metrics(path, DAY) is None
    _publish(path)
    actual = read_edinet_metrics(path, DAY)
    assert actual is not None
    assert actual["9682"].fcf_ttm == 700
    assert read_edinet_metric_baseline(path, prev) == earlier


def test_failed_attempt_cannot_write_metric_payload(tmp_path):
    path = tmp_path / "market.sqlite"
    _publish(path)
    original = path.read_bytes()
    with pytest.raises(ValueError, match="failed EDINET metric attempts cannot write metric rows"):
        store_edinet_metrics(
            path, DAY, [{"ticker": "9682", "extractor_revision": "fixture"}], status="failed"
        )
    assert path.read_bytes() == original


@pytest.mark.parametrize("include_invalid", [False, True])
def test_unpublishable_rows_cannot_replace_valid_baseline(tmp_path, include_invalid):
    path = tmp_path / "market.sqlite"
    valid = {"ticker": "9682", "cash": 11.0, "extractor_revision": "fixture"}
    assert store_edinet_metrics(path, DAY, [valid]) == 1
    baseline = read_edinet_metric_baseline(path, DAY)
    assert baseline is not None
    assert baseline.rows["9682"].record.cash == 11.0

    replacement = []
    if include_invalid:
        replacement = [{**valid, "ticker": "not-a-ticker"}, {**valid, "cash": 99.0}]
    message = "invalid ticker" if include_invalid else "at least one row"
    with pytest.raises(ValueError, match=message):
        store_edinet_metrics(path, DAY, replacement)
    assert read_edinet_metric_baseline(path, DAY) == baseline
