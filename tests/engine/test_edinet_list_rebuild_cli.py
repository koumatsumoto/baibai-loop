"""Offline EDINET list replay runs through the real CLI without provider setup."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import requests
from tests.helpers.edinet_documents import store_edinet_documents

from baibai_engine.data_collection.edinet.receipts import retain_list_response
from baibai_engine.source_maintenance import cli as app
from baibai_engine.storage.observations.schema import open_connection


def _wire(rows: list[dict[str, object]]) -> bytes:
    return json.dumps({"metadata": {"resultset": {"count": len(rows)}}, "results": rows}).encode()


def _retain(path: Path, day: date, received: datetime, body: bytes) -> str:
    with closing(open_connection(path)) as connection:
        return retain_list_response(connection, doc_date=day, received_at=received, body=body)


def _read(path: Path, sql: str, params: tuple[str, ...] = ()) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(sql, params).fetchall()


@pytest.fixture(autouse=True)
def _offline_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("offline replay must not configure or create a provider/session")

    monkeypatch.setattr(app, "load_project_env", lambda: None)
    monkeypatch.setattr(
        app,
        "SourceMaintenanceConfig",
        type("ForbiddenConfig", (), {"from_env": staticmethod(forbidden)}),
    )
    monkeypatch.setattr(app, "EDINETRefresh", forbidden)
    monkeypatch.setattr(app, "JQuantsRefresh", forbidden)
    monkeypatch.setattr(requests, "Session", forbidden)


def _run(path: Path, start: str, end: str) -> int:
    return app.main(
        [
            "rebuild-edinet-lists",
            "--start",
            start,
            "--end",
            end,
            "--sqlite-path",
            str(path),
        ]
    )


def test_range_replay_carries_origin_rechecks_from_retained_proof_and_preserves_l1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "market.sqlite"
    outside = date(2026, 10, 14)
    historical = date(2026, 10, 15)
    proof_day = date(2026, 10, 16)
    store_edinet_documents(path, outside, [{"seqNumber": 1, "docID": "S100OUT"}])
    original_outside = _read(
        path, "SELECT * FROM edinet_document_lists WHERE doc_date = ?", (outside.isoformat(),)
    )
    first = {
        "seqNumber": 1,
        "docID": "S100CARRY",
        "legalStatus": "1",
        "docDescription": "原届出",
    }
    expired = {**first, "legalStatus": "0", "docDescription": None}
    _retain(path, historical, datetime(2026, 10, 15, 0, tzinfo=UTC), _wire([first]))
    _retain(path, proof_day, datetime(2026, 10, 16, 0, tzinfo=UTC), _wire([]))
    selected_id = _retain(path, historical, datetime(2026, 10, 16, 1, tzinfo=UTC), _wire([expired]))
    receipts_before = _read(
        path,
        "SELECT receipt_id, doc_date, received_at_utc, body_gzip "
        "FROM edinet_list_responses ORDER BY receipt_id",
    )

    assert _run(path, historical.isoformat(), proof_day.isoformat()) == 0
    assert "days_succeeded=2 days_failed=0 projected_rows=1" in capsys.readouterr().out
    assert _read(
        path,
        "SELECT doc_id, doc_description, legal_status FROM edinet_documents WHERE doc_date = ?",
        (historical.isoformat(),),
    ) == [("S100CARRY", "原届出", "0")]
    assert _read(
        path,
        "SELECT selected_receipt_id, selected_received_at_utc, "
        "latest_received_at_utc, rechecked_asof FROM edinet_document_lists "
        "WHERE doc_date = ?",
        (historical.isoformat(),),
    ) == [
        (
            selected_id,
            "2026-10-16T01:00:00.000000+00:00",
            "2026-10-16T01:00:00.000000+00:00",
            "2026-10-16",
        )
    ]
    assert _read(
        path,
        "SELECT result_count, rechecked_asof FROM edinet_document_lists WHERE doc_date = ?",
        (proof_day.isoformat(),),
    ) == [(0, None)]
    assert (
        _read(
            path, "SELECT * FROM edinet_document_lists WHERE doc_date = ?", (outside.isoformat(),)
        )
        == original_outside
    )
    assert _read(
        path, "SELECT doc_id FROM edinet_documents WHERE doc_date = ?", (outside.isoformat(),)
    ) == [("S100OUT",)]
    assert (
        _read(
            path,
            "SELECT receipt_id, doc_date, received_at_utc, body_gzip "
            "FROM edinet_list_responses ORDER BY receipt_id",
        )
        == receipts_before
    )


def test_missing_middle_day_preflight_leaves_existing_l2_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "market.sqlite"
    first = date(2026, 10, 15)
    last = date(2026, 10, 17)
    store_edinet_documents(path, first, [{"seqNumber": 1, "docID": "S100OLD"}])
    _retain(path, first, datetime(2026, 10, 15, tzinfo=UTC), _wire([]))
    _retain(path, last, datetime(2026, 10, 17, tzinfo=UTC), _wire([]))
    before = _read(path, "SELECT * FROM edinet_document_lists ORDER BY doc_date")
    documents_before = _read(path, "SELECT * FROM edinet_documents ORDER BY doc_date")

    assert _run(path, first.isoformat(), last.isoformat()) == 1
    assert "missing raw EDINET list receipts: 2026-10-16" in capsys.readouterr().err
    assert _read(path, "SELECT * FROM edinet_document_lists ORDER BY doc_date") == before
    assert _read(path, "SELECT * FROM edinet_documents ORDER BY doc_date") == documents_before


def test_malformed_day_persists_failed_coverage_and_continues(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "market.sqlite"
    start = date(2026, 10, 15)
    middle = date(2026, 10, 16)
    end = date(2026, 10, 17)
    _retain(
        path,
        start,
        datetime(2026, 10, 15, tzinfo=UTC),
        _wire([{"seqNumber": 1, "docID": "S100GOOD"}]),
    )
    _retain(path, middle, datetime(2026, 10, 16, tzinfo=UTC), b"{bad JSON")
    _retain(path, end, datetime(2026, 10, 17, tzinfo=UTC), _wire([]))

    assert _run(path, start.isoformat(), end.isoformat()) == 1
    captured = capsys.readouterr()
    assert "days_succeeded=2 days_failed=1 projected_rows=1" in captured.out
    assert "2026-10-16: raw projection failed:" in captured.err
    assert _read(
        path,
        "SELECT coverage_key, status, record_count FROM source_coverage "
        "WHERE source = 'edinet_documents' ORDER BY coverage_key",
    ) == [
        ("2026-10-15", "ok", 1),
        ("2026-10-16", "failed", 0),
        ("2026-10-17", "ok", 0),
    ]
    assert _read(
        path,
        "SELECT selected_received_at_utc, latest_received_at_utc, "
        "rechecked_asof FROM edinet_document_lists WHERE doc_date = ?",
        (middle.isoformat(),),
    ) == [(None, "2026-10-16T00:00:00.000000+00:00", None)]


def test_recheck_is_not_inferred_without_a_retained_proof_receipt(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    day = date(2026, 10, 15)
    _retain(path, day, datetime(2026, 10, 16, 1, tzinfo=UTC), _wire([]))
    assert _run(path, day.isoformat(), day.isoformat()) == 0
    assert _read(
        path,
        "SELECT rechecked_asof FROM edinet_document_lists WHERE doc_date = ?",
        (day.isoformat(),),
    ) == [(None,)]


def test_operation_row_preserves_explicit_null_and_omitted_keys_in_native_cache(
    tmp_path: Path,
) -> None:
    path = tmp_path / "market.sqlite"
    day = date(2026, 10, 15)
    operation = {
        "seqNumber": 1,
        "docID": "S100EDIT",
        "docInfoEditStatus": "1",
        "docDescription": None,
    }
    _retain(path, day, datetime(2026, 10, 15, tzinfo=UTC), _wire([operation]))

    assert _run(path, day.isoformat(), day.isoformat()) == 0
    raw_json = _read(
        path,
        "SELECT response_json FROM edinet_document_lists WHERE doc_date = ?",
        (day.isoformat(),),
    )[0][0]
    native = json.loads(raw_json)
    saved_operation = native["results"][0]
    assert saved_operation["docDescription"] is None
    assert "secCode" not in saved_operation
    assert "doc_date" not in saved_operation


def test_corrupt_raw_bytes_return_failure_without_success_coverage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "market.sqlite"
    day = date(2026, 10, 15)
    receipt_id = _retain(path, day, datetime(2026, 10, 15, tzinfo=UTC), _wire([]))
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "UPDATE edinet_list_responses SET body_gzip = ? WHERE receipt_id = ?",
            (b"not gzip", receipt_id),
        )
        connection.commit()

    assert _run(path, day.isoformat(), day.isoformat()) == 1
    captured = capsys.readouterr()
    assert "days_succeeded=0 days_failed=1 projected_rows=0" in captured.out
    assert "rebuild failed" in captured.err
    assert _read(path, "SELECT * FROM edinet_document_lists") == []
    assert _read(path, "SELECT * FROM source_coverage WHERE source = 'edinet_documents'") == []


def test_sql_write_failure_is_nonzero_and_does_not_claim_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "market.sqlite"
    day = date(2026, 10, 15)
    _retain(path, day, datetime(2026, 10, 15, tzinfo=UTC), _wire([]))
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TRIGGER reject_rebuild BEFORE INSERT ON edinet_document_lists "
            "BEGIN SELECT RAISE(ABORT, 'synthetic SQL failure'); END"
        )
        connection.commit()

    assert _run(path, day.isoformat(), day.isoformat()) == 1
    captured = capsys.readouterr()
    assert "days_succeeded=0 days_failed=1 projected_rows=0" in captured.out
    assert "synthetic SQL failure" in captured.err
    assert _read(path, "SELECT * FROM edinet_document_lists") == []
    assert _read(path, "SELECT * FROM source_coverage WHERE source = 'edinet_documents'") == []


def test_invalid_range_and_missing_or_obsolete_database_do_not_create_l2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    absent = tmp_path / "missing.sqlite"
    assert _run(absent, "2026-10-17", "2026-10-15") == 1
    assert "--start must be on or before --end" in capsys.readouterr().err
    assert _run(absent, "2026-10-15", "2026-10-17") == 1
    assert "database is missing" in capsys.readouterr().err
    assert not absent.exists()
    with closing(sqlite3.connect(absent)) as connection:
        connection.execute("PRAGMA user_version = 42")
    before = absent.read_bytes()
    assert _run(absent, "2026-10-15", "2026-10-17") == 1
    assert "preflight failed" in capsys.readouterr().err
    assert absent.read_bytes() == before
