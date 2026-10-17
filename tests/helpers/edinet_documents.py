"""Direct L2 seed for reader fixtures; production lists are rebuilt from receipts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.storage.observations.coverage import (
    date_range_row_count,
    delete_overlapping_source_coverage,
    record_source_coverage,
)
from baibai_engine.storage.observations.ingest.edinet import _edinet_document_rows
from baibai_engine.storage.observations.schema import open_connection


def store_edinet_documents(
    db_path: Path,
    on_date: date,
    records: Iterable[Mapping[str, Any]],
    *,
    process_datetime: str | None = None,
    result_count: int | None = None,
    is_final: bool = False,
    response_json: str | None = None,
) -> int:
    """Seed normalized fixtures without using them as projection evidence."""
    records_list = list(records)
    expected_count = len(records_list) if result_count is None else result_count
    if expected_count != len(records_list):
        raise ValueError(
            "EDINET document resultset count mismatch: "
            f"metadata={expected_count} results={len(records_list)}"
        )
    rows = _edinet_document_rows(on_date.isoformat(), records_list)
    conn = open_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM edinet_documents WHERE doc_date = ?", (on_date.isoformat(),))
        delete_overlapping_source_coverage(conn, "edinet_documents", on_date, on_date)
        if rows:
            conn.executemany(
                "INSERT INTO edinet_documents("
                "doc_date, sequence_number, doc_id, sec_code, doc_type_code, csv_flag, "
                "xbrl_flag, legal_status, disclosure_status, withdrawal_status, "
                "doc_info_edit_status, parent_doc_id, operation_datetime, submit_datetime, "
                "doc_description, period_start, period_end, edinet_code, "
                "issuer_edinet_code, subject_edinet_code"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        persisted_count = date_range_row_count(
            conn, "edinet_documents", "doc_date", on_date, on_date
        )
        record_source_coverage(
            conn,
            source="edinet_documents",
            coverage_key=on_date.isoformat(),
            coverage_start=on_date.isoformat(),
            coverage_end=on_date.isoformat(),
            record_count=persisted_count,
            status="ok",
            error=None,
        )
        conn.execute(
            "INSERT OR REPLACE INTO edinet_document_lists("
            "doc_date, process_datetime, result_count, fetched_at_utc, is_final, response_json"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                on_date.isoformat(),
                process_datetime,
                expected_count,
                datetime.now(UTC).isoformat(),
                int(is_final),
                response_json,
            ),
        )
        conn.commit()
        return persisted_count
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
