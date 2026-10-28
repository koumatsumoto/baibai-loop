from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from tests.helpers.edinet_documents import store_edinet_documents

from baibai_engine.data_collection.edinet.receipts import read_list_responses, retain_list_response
from baibai_engine.machine_analysis.observations.disclosures.edinet_document_store import (
    read_current_filing_documents,
    read_stored_documents,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_list_projection import (
    document_list_rechecked_asof,
    project_document_list,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_list_rebuild import store_edinet_document_projection
from baibai_engine.market.edinet_metrics.store import read_edinet_documents
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.storage.observations.schema import open_connection

_DAY = date(2026, 10, 15)
_STAMP = datetime(2026, 10, 15, 9, tzinfo=UTC)


def _wire(rows: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {
            "metadata": {"resultset": {"count": len(rows)}, "processDateTime": "2026-10-15 12:00"},
            "results": rows,
        },
        ensure_ascii=False,
    ).encode()


_A = _wire([{"seqNumber": 1, "docID": "S100TEST", "legalStatus": "1", "docDescription": "原届出"}])
_B = b"{invalid JSON"
_C = _wire([{"seqNumber": 1, "docID": "S100TEST", "legalStatus": "0"}])
_EMPTY = _wire([])


def _add_receipt(path: Path, body: bytes, *, seconds: int) -> str:
    with closing(open_connection(path)) as conn:
        return retain_list_response(
            conn, doc_date=_DAY, received_at=_STAMP + timedelta(seconds=seconds), body=body
        )


def _copy_receipts(source: Path, target: Path, ids: tuple[str, ...]) -> None:
    with closing(open_connection(source)) as origin, closing(open_connection(target)) as dest:
        for receipt_id in ids:
            row = origin.execute(
                "SELECT receipt_id, doc_date, received_at_utc, body_gzip "
                "FROM edinet_list_responses WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
            assert row is not None
            dest.execute(
                "INSERT INTO edinet_list_responses("
                "receipt_id, doc_date, received_at_utc, body_gzip) VALUES (?, ?, ?, ?)",
                row,
            )
        dest.commit()


def _project_and_store(path: Path) -> int:
    with closing(open_connection(path)) as conn:
        projection = project_document_list(read_list_responses(conn, doc_date=_DAY))
    return store_edinet_document_projection(path, projection, rechecked_asof=None)


def _snapshot(path: Path) -> tuple[object, ...]:
    with closing(open_connection(path)) as conn:
        conn.row_factory = sqlite3.Row
        documents = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM edinet_documents WHERE doc_date = ? ORDER BY sequence_number",
                (_DAY.isoformat(),),
            ).fetchall()
        ]
        metadata_row = conn.execute(
            "SELECT process_datetime, result_count, selected_received_at_utc, "
            "response_json, selected_receipt_id, latest_received_at_utc, rechecked_asof "
            "FROM edinet_document_lists WHERE doc_date = ?",
            (_DAY.isoformat(),),
        ).fetchone()
        metadata = dict(metadata_row) if metadata_row is not None else None
        coverage_row = conn.execute(
            "SELECT coverage_key, coverage_start, coverage_end, fetched_at_utc, "
            "record_count, status, error FROM source_coverage "
            "WHERE source = 'edinet_documents' AND coverage_key = ?",
            (_DAY.isoformat(),),
        ).fetchone()
        coverage = tuple(coverage_row) if coverage_row is not None else None
    return documents, metadata, coverage


def test_clean_and_incremental_replay_have_identical_failed_state_and_recover(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite"
    a = _add_receipt(source, _A, seconds=1)
    b = _add_receipt(source, _B, seconds=2)
    c = _add_receipt(source, _C, seconds=3)
    clean = tmp_path / "clean.sqlite"
    incremental = tmp_path / "incremental.sqlite"
    _copy_receipts(source, clean, (a, b))
    _copy_receipts(source, incremental, (a,))
    assert _project_and_store(incremental) == 1
    _copy_receipts(source, incremental, (b,))

    assert _project_and_store(clean) == _project_and_store(incremental) == 1
    assert _snapshot(clean) == _snapshot(incremental)
    documents, metadata, coverage = _snapshot(clean)
    assert documents[0]["doc_id"] == "S100TEST"
    assert metadata["selected_receipt_id"] == a
    assert metadata["process_datetime"] == "2026-10-15 12:00"
    assert metadata["result_count"] == 1
    assert metadata["selected_received_at_utc"] == (_STAMP + timedelta(seconds=1)).isoformat(
        timespec="microseconds"
    )
    assert metadata["latest_received_at_utc"] == (_STAMP + timedelta(seconds=2)).isoformat(
        timespec="microseconds"
    )
    assert metadata["rechecked_asof"] is None
    assert coverage[5] == "failed"
    assert "JSONDecodeError" in coverage[6]
    assert read_edinet_documents(clean, _DAY) is None
    with closing(open_connection(clean)) as conn:
        with pytest.raises(EDINETProviderError, match=_DAY.isoformat()):
            read_stored_documents(conn, through=_DAY)
        with pytest.raises(EDINETProviderError, match=_DAY.isoformat()):
            read_current_filing_documents(conn, through=_DAY)
        assert (
            read_stored_documents(conn, through=date(2026, 10, 16), start=date(2026, 10, 16)) == []
        )

    _copy_receipts(source, clean, (c,))
    _copy_receipts(source, incremental, (c,))
    assert _project_and_store(clean) == _project_and_store(incremental) == 1
    assert _snapshot(clean) == _snapshot(incremental)
    assert _snapshot(clean)[2][5] == "ok"
    assert read_edinet_documents(clean, _DAY)[0]["docDescription"] == "原届出"
    with closing(open_connection(source)) as origin, closing(open_connection(clean)) as target:
        assert (
            target.execute(
                "SELECT receipt_id, body_gzip FROM edinet_list_responses ORDER BY receipt_id"
            ).fetchall()
            == origin.execute(
                "SELECT receipt_id, body_gzip FROM edinet_list_responses ORDER BY receipt_id"
            ).fetchall()
        )


def test_successful_empty_replay_clears_previous_documents(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    _add_receipt(path, _A, seconds=1)
    assert _project_and_store(path) == 1
    empty_id = _add_receipt(path, _EMPTY, seconds=2)
    assert _project_and_store(path) == 0
    documents, metadata, coverage = _snapshot(path)
    assert documents == []
    assert metadata["result_count"] == 0
    assert metadata["selected_receipt_id"] == empty_id
    assert coverage[5] == "ok"
    assert read_edinet_documents(path, _DAY) == []


@pytest.mark.parametrize(
    "body",
    [
        _B,
        b'{"metadata":{"resultset":{"count":0}},"results":[],"unused":1e999}',
        b'{"metadata":{"resultset":{"count":0}},"results":[],"unused":-1e999}',
    ],
    ids=["invalid-json", "positive-overflow", "negative-overflow"],
)
def test_only_malformed_receipt_writes_failed_coverage_and_attempt_time(
    tmp_path: Path, body: bytes
) -> None:
    path = tmp_path / "market.sqlite"
    _add_receipt(path, body, seconds=1)
    with closing(open_connection(path)) as conn:
        projection = project_document_list(read_list_responses(conn, doc_date=_DAY))
    assert (
        store_edinet_document_projection(path, projection, rechecked_asof=date(2026, 10, 16)) == 0
    )
    documents, metadata, coverage = _snapshot(path)
    assert documents == []
    assert metadata["result_count"] == 0
    assert metadata["selected_received_at_utc"] is None
    assert metadata["response_json"] is None
    assert metadata["selected_receipt_id"] is None
    assert metadata["latest_received_at_utc"] == (_STAMP + timedelta(seconds=1)).isoformat(
        timespec="microseconds"
    )
    assert metadata["rechecked_asof"] is None
    assert coverage[5] == "failed"
    assert read_edinet_documents(path, _DAY) is None


def test_ambiguous_latest_receipts_block_current_reader(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    selected_id = _add_receipt(path, _A, seconds=1)
    _add_receipt(path, _C, seconds=2)
    _add_receipt(path, _EMPTY, seconds=2)
    assert _project_and_store(path) == 1
    _documents, metadata, coverage = _snapshot(path)
    assert metadata["selected_receipt_id"] == selected_id
    assert metadata["rechecked_asof"] is None
    assert coverage[5] == "failed"
    assert "ambiguous" in coverage[6]
    assert read_edinet_documents(path, _DAY) is None


def test_writer_sets_recheck_fields_from_proof_result(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    selected_at = datetime(2026, 10, 15, 15, 1, tzinfo=UTC)
    proof_at = selected_at - timedelta(minutes=1)
    with closing(open_connection(path)) as conn:
        retain_list_response(conn, doc_date=_DAY, received_at=selected_at, body=_A)
        retain_list_response(conn, doc_date=date(2026, 10, 16), received_at=proof_at, body=_EMPTY)
        projection = project_document_list(read_list_responses(conn, doc_date=_DAY))
        proof = document_list_rechecked_asof(
            projection, read_list_responses(conn, doc_date=date(2026, 10, 16))
        )
    assert proof == date(2026, 10, 16)
    store_edinet_document_projection(path, projection, rechecked_asof=proof)
    metadata = _snapshot(path)[1]
    assert metadata["rechecked_asof"] == "2026-10-16"


def test_historical_carry_uses_only_receipts_not_seeded_l2_rows(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    store_edinet_documents(
        path,
        _DAY,
        [{"seqNumber": 1, "docID": "S100TEST", "legalStatus": "1", "docDescription": "WRONG"}],
    )
    _add_receipt(path, _C, seconds=1)
    assert _project_and_store(path) == 1
    with closing(open_connection(path)) as conn:
        assert conn.execute(
            "SELECT doc_description FROM edinet_documents WHERE doc_date = ?",
            (_DAY.isoformat(),),
        ).fetchone() == (None,)
    assert read_edinet_documents(path, _DAY)[0]["docDescription"] is None


def test_failed_sql_write_rolls_back_rows_metadata_and_coverage(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    _add_receipt(path, _A, seconds=1)
    _project_and_store(path)
    before = _snapshot(path)
    _add_receipt(path, _C, seconds=2)
    with closing(open_connection(path)) as conn:
        receipt_bytes_before = conn.execute(
            "SELECT receipt_id, body_gzip FROM edinet_list_responses ORDER BY receipt_id"
        ).fetchall()
        projection = project_document_list(read_list_responses(conn, doc_date=_DAY))
        conn.execute(
            "CREATE TRIGGER reject_projection BEFORE INSERT ON edinet_document_lists "
            "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
        conn.commit()
    with pytest.raises(sqlite3.DatabaseError, match="synthetic failure"):
        store_edinet_document_projection(path, projection, rechecked_asof=None)
    assert _snapshot(path) == before
    with closing(open_connection(path)) as conn:
        assert (
            conn.execute(
                "SELECT receipt_id, body_gzip FROM edinet_list_responses ORDER BY receipt_id"
            ).fetchall()
            == receipt_bytes_before
        )


def test_failed_serialization_preserves_previous_rows_and_coverage(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    _add_receipt(path, _A, seconds=1)
    _project_and_store(path)
    before = _snapshot(path)
    with closing(open_connection(path)) as conn:
        projection = project_document_list(read_list_responses(conn, doc_date=_DAY))
    assert projection.native_payload is not None
    broken = replace(
        projection, native_payload={**projection.native_payload, "unused": float("nan")}
    )
    with pytest.raises(ValueError, match="Out of range float"):
        store_edinet_document_projection(path, broken, rechecked_asof=None)
    assert _snapshot(path) == before
