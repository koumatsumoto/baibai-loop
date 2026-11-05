"""CSV acquisition keeps its source scope independently of extracted metric state."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from tests.helpers.edinet_csv_zip import _edinet_csv_zip

from baibai_engine.data_collection.providers.edinet.archives import retain_source_archive
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.data_collection.providers.edinet.receipts import retain_list_response
from baibai_engine.machine_analysis.observations.disclosures.edinet_list_rebuild import (
    rebuild_edinet_document_day,
)
from baibai_engine.storage.sqlite.source_cache.coverage import record_source_coverage
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition import edinet_archives as subject
from baibai_jobs.acquisition.inputs import export_source_input

ASOF = date(2026, 11, 5)
START = ASOF - timedelta(days=540)
AT = datetime(2026, 11, 5, 12, tzinfo=UTC)


def filing(doc_id="S100CURRENT", ticker="72030", **values):
    return {
        "seqNumber": 1,
        "docID": doc_id,
        "secCode": ticker,
        "docTypeCode": "120",
        "csvFlag": "1",
        "xbrlFlag": "1",
        "legalStatus": "1",
        "disclosureStatus": "0",
        "withdrawalStatus": "0",
        "docInfoEditStatus": "0",
        "submitDateTime": str(ASOF) + " 10:00",
        "periodStart": "2025-04-01",
        "periodEnd": "2026-03-31",
        **values,
    }


def save_list(path, day, documents):
    body = json.dumps(
        {"metadata": {"resultset": {"count": len(documents)}}, "results": documents}
    ).encode()
    with closing(open_connection(path)) as connection:
        retain_list_response(connection, doc_date=day, received_at=AT, body=body)
    rebuild_edinet_document_day(path, day)


def complete_window(path):
    # The existing input contract accepts coverage of normally empty days.
    with closing(open_connection(path)) as connection, connection:
        record_source_coverage(
            connection,
            source="edinet_documents",
            coverage_key="window",
            coverage_start=str(START),
            coverage_end=str(ASOF),
            record_count=1,
        )


def l2_rows(path):
    with sqlite3.connect(path) as connection:
        return (
            connection.execute("SELECT * FROM edinet_metrics").fetchall(),
            connection.execute(
                "SELECT * FROM source_coverage WHERE source='edinet_metrics'"
            ).fetchall(),
        )


@pytest.mark.parametrize("metric_state", ["missing", "ok", "failed", "corrupt", "old_revision"])
@pytest.mark.parametrize("retained", [False, True])
def test_metric_state_never_controls_archive_get_or_changes_during_acquisition(
    tmp_path, monkeypatch, metric_state, retained
):
    path = tmp_path / "source.sqlite"
    save_list(path, ASOF, [filing()])
    complete_window(path)
    with closing(open_connection(path)) as connection, connection:
        if metric_state != "missing":
            connection.execute(
                "INSERT INTO edinet_metrics(asof_date,ticker,source_doc_id,failure_reasons,extractor_revision) "
                "VALUES (?,?,?,?,?)",
                (
                    str(ASOF),
                    "7203",
                    "S100CURRENT",
                    "not-json" if metric_state == "corrupt" else "[]",
                    metric_state,
                ),
            )
            record_source_coverage(
                connection,
                source="edinet_metrics",
                coverage_key=str(ASOF),
                coverage_start=str(ASOF),
                coverage_end=str(ASOF),
                record_count=1,
                status="failed" if metric_state == "failed" else "ok",
            )
    body = _edinet_csv_zip([])
    if retained:
        with closing(open_connection(path)) as connection:
            retain_source_archive(connection, "S100CURRENT", 5, body, stored_on=START)
    before = l2_rows(path)
    connect = subject.connect_read_only

    def without_metric_reads(path):
        connection = connect(path)
        connection.set_authorizer(
            lambda action, table, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "edinet_metrics"
                else sqlite3.SQLITE_OK
            )
        )
        return connection

    monkeypatch.setattr(subject, "connect_read_only", without_metric_reads)
    with patch("baibai_jobs.maintenance.source_refresh.edinet.EDINETHTTP") as http:
        http.return_value.fetch_archive.return_value = EdinetResponse(received_at=AT, body=body)
        report = subject.acquire_archives(
            path,
            group="edinet-metrics",
            asof=ASOF,
            api_key="synthetic",
            retained={("S100CURRENT", 5)} if retained else set(),
        )
        assert http.return_value.fetch_archive.call_count == int(not retained)
        if not retained:
            http.return_value.fetch_archive.assert_called_once_with("S100CURRENT", 5)
        http.return_value.fetch_list.assert_not_called()
    assert report["status"] == "received"
    assert report["downloaded"] == int(not retained)
    assert report["retained_archives_reused"] == int(retained)
    assert l2_rows(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT doc_id,source_type,stored_on,zip_content FROM edinet_source_archives"
        ).fetchall() == [("S100CURRENT", 5, str(START if retained else AT.date()), body)]


def test_window_is_inclusive_and_old_origins_only_resolve_operations(tmp_path):
    path = tmp_path / "source.sqlite"
    save_list(path, START - timedelta(days=1), [filing("S100OLD", "99990")])
    save_list(path, START, [filing("S100START", "72030")])
    save_list(
        path,
        ASOF,
        [
            filing("S100END", "83060"),
            {
                "seqNumber": 2,
                "docID": "S100OLD",
                "docInfoEditStatus": "1",
                "opeDateTime": str(ASOF) + " 11:00",
                "docDescription": "older origin edit",
            },
        ],
    )
    save_list(path, ASOF + timedelta(days=1), [filing("S100FUTURE", "67580")])
    complete_window(path)
    assert subject.metric_archive_candidates(path, asof=ASOF) == (["S100START", "S100END"], 0)


@pytest.mark.parametrize("problem", ["missing", "failed", "no_candidates"])
def test_incomplete_or_empty_selection_fails_without_get_or_l2_failure_write(tmp_path, problem):
    path = tmp_path / "source.sqlite"
    save_list(path, ASOF, [] if problem == "no_candidates" else [filing()])
    if problem != "missing":
        complete_window(path)
    if problem == "failed":
        with closing(open_connection(path)) as connection, connection:
            record_source_coverage(
                connection,
                source="edinet_documents",
                coverage_key="failed_day",
                coverage_start=str(START),
                coverage_end=str(START),
                record_count=0,
                status="failed",
                error="source list unavailable",
            )
    before = path.read_bytes()
    with patch("baibai_jobs.maintenance.source_refresh.edinet.EDINETHTTP") as http:
        report = subject.acquire_archives(
            path, group="edinet-metrics", asof=ASOF, api_key="synthetic", retained=set()
        )
        http.assert_not_called()
    assert report == {"status": "failed", "failed": ["selection"]}
    assert path.read_bytes() == before


def test_native_quarantine_isolated_but_metric_only_issuer_identity_is_not_inferred(tmp_path):
    path = tmp_path / "source.sqlite"
    save_list(path, START, [filing("S100SAFE", "83060"), filing("S100BLOCK", "72030", seqNumber=2)])
    save_list(
        path,
        ASOF,
        [
            filing("S100MISSING", "72030", docInfoEditStatus="1", opeDateTime=str(ASOF) + " 11:00"),
            {
                "seqNumber": 2,
                "docID": "S100UNKNOWN",
                "docInfoEditStatus": "1",
                "opeDateTime": str(ASOF) + " 11:00",
            },
        ],
    )
    complete_window(path)
    with closing(open_connection(path)) as connection, connection:
        connection.execute(
            "INSERT INTO edinet_metrics(asof_date,ticker,source_doc_id,document_type) VALUES (?,?,?,?)",
            (str(ASOF), "8306", "S100UNKNOWN", "120"),
        )
    assert subject.metric_archive_candidates(path, asof=ASOF) == (["S100SAFE"], 2)


def test_new_input_excludes_metric_l2_and_keeps_native_identity_and_facts_coverage(tmp_path):
    canonical, scoped = tmp_path / "canonical.sqlite", tmp_path / "scoped.sqlite"
    save_list(canonical, ASOF, [filing()])
    complete_window(canonical)
    with closing(open_connection(canonical)) as connection:
        retain_source_archive(connection, "S100CURRENT", 5, b"unchanged bytes", stored_on=START)
        connection.execute(
            "INSERT INTO edinet_metrics(asof_date,ticker,failure_reasons) VALUES (?,?,?)",
            (str(ASOF), "7203", "not-json"),
        )
        for source in ("edinet_metrics", "edinet_research_facts"):
            record_source_coverage(
                connection,
                source=source,
                coverage_key="initialized",
                coverage_start=str(START),
                coverage_end=str(ASOF),
                record_count=1,
            )
        connection.commit()
    report = export_source_input(canonical, scoped, group="edinet-metrics", asof=ASOF)
    assert "edinet_metrics" not in report["rows"]
    assert report["archive_keys"] == [("S100CURRENT", 5)]
    with sqlite3.connect(scoped) as connection:
        assert connection.execute("SELECT count(*) FROM edinet_metrics").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM source_coverage WHERE source='edinet_metrics'"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT count(*) FROM edinet_list_responses").fetchone()[0] == 1
    assert subject.metric_archive_candidates(scoped, asof=ASOF) == (["S100CURRENT"], 0)
    facts = tmp_path / "facts.sqlite"
    export_source_input(canonical, facts, group="edinet-facts", asof=ASOF)
    with sqlite3.connect(facts) as connection:
        assert connection.execute(
            "SELECT coverage_key FROM source_coverage WHERE source='edinet_research_facts'"
        ).fetchall() == [("initialized",)]


def test_old_parent_withdrawal_excludes_current_correction_without_losing_saved_zip(tmp_path):
    path = tmp_path / "source.sqlite"
    save_list(path, START - timedelta(days=1), [filing("S100PARENT", "72030")])
    save_list(
        path, START, [filing("S100CHILD", "72030", docTypeCode="130", parentDocID="S100PARENT")]
    )
    save_list(
        path,
        ASOF,
        [
            filing("S100SAFE", "83060"),
            {
                "seqNumber": 2,
                "docID": "S100WITHDRAW",
                "parentDocID": "S100PARENT",
                "withdrawalStatus": "1",
                "opeDateTime": str(ASOF) + " 11:00",
            },
        ],
    )
    complete_window(path)
    with closing(open_connection(path)) as connection:
        retain_source_archive(connection, "S100CHILD", 5, b"retained original", stored_on=START)
    before = path.read_bytes()
    assert subject.metric_archive_candidates(path, asof=ASOF) == (["S100SAFE"], 0)
    assert path.read_bytes() == before


def test_xbrl_identity_does_not_satisfy_csv_original(tmp_path):
    path = tmp_path / "source.sqlite"
    save_list(path, ASOF, [filing()])
    complete_window(path)
    body = _edinet_csv_zip([])
    with closing(open_connection(path)) as connection:
        retain_source_archive(connection, "S100CURRENT", 1, b"existing xbrl", stored_on=START)
    with patch("baibai_jobs.maintenance.source_refresh.edinet.EDINETHTTP") as http:
        http.return_value.fetch_archive.return_value = EdinetResponse(received_at=AT, body=body)
        report = subject.acquire_archives(
            path,
            group="edinet-metrics",
            asof=ASOF,
            api_key="synthetic",
            retained={("S100CURRENT", 1)},
        )
        http.return_value.fetch_archive.assert_called_once_with("S100CURRENT", 5)
    assert report["downloaded"] == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT source_type,zip_content FROM edinet_source_archives ORDER BY source_type"
        ).fetchall() == [
            (1, b"existing xbrl"),
            (5, body),
        ]
