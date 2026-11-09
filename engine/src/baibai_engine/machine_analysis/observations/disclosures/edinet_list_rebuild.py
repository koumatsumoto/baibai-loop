"""Materialize EDINET lists from retained source receipts without network access."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, date, timedelta
from pathlib import Path
from typing import TextIO
from zoneinfo import ZoneInfo

from baibai_engine.data_collection.providers.edinet.document_rows import (
    document_rows,
    document_text,
)
from baibai_engine.data_collection.providers.edinet.list_projection import (
    DocumentListProjection,
    document_list_rechecked_asof,
    project_document_list,
)
from baibai_engine.data_collection.providers.edinet.models import EDINETProviderError
from baibai_engine.data_collection.providers.edinet.receipts import read_list_responses
from baibai_engine.storage.sqlite.source_cache.coverage import (
    date_range_row_count,
    delete_overlapping_source_coverage,
    record_source_coverage,
)
from baibai_engine.storage.sqlite.source_cache.schema import (
    SQLiteSchemaError,
    connect_current,
    open_connection,
)

_JST = ZoneInfo("Asia/Tokyo")


def pending_edinet_document_dates(
    connection: sqlite3.Connection, *, through: date
) -> tuple[date, ...]:
    """Retained receipts not yet represented by an eligible list projection."""
    return tuple(
        date.fromisoformat(row[0])
        for row in connection.execute(
            "SELECT DISTINCT r.doc_date FROM edinet_list_responses r "
            "LEFT JOIN edinet_document_lists p ON p.doc_date = r.doc_date "
            "WHERE r.doc_date <= ? AND (p.doc_date IS NULL "
            "OR r.received_at_utc > p.latest_received_at_utc "
            "OR (r.received_at_utc = p.latest_received_at_utc "
            "AND r.receipt_id <> p.selected_receipt_id) "
            "OR EXISTS (SELECT 1 FROM source_coverage c "
            "WHERE c.source = 'edinet_documents' AND c.status = 'failed' "
            "AND c.coverage_start <= r.doc_date AND c.coverage_end >= r.doc_date)) "
            "ORDER BY r.doc_date",
            (through.isoformat(),),
        )
    )


def rebuild_edinet_document_day(
    sqlite_path: Path, on_date: date, *, connection: sqlite3.Connection | None = None
) -> DocumentListProjection:
    """Save current success or failed coverage, then return the deterministic result."""
    if not sqlite_path.exists():
        raise EDINETProviderError("EDINET retained source database is missing")
    conn = connection if connection is not None else connect_current(sqlite_path)
    if conn is None:
        raise EDINETProviderError("EDINET retained source database is unavailable")
    try:
        if connection is None:
            conn.execute("BEGIN")
        receipts = read_list_responses(conn, doc_date=on_date)
        if not receipts:
            raise EDINETProviderError(f"EDINET list receipts are missing: {on_date.isoformat()}")
        projection = project_document_list(receipts)
        selected = projection.selected_receipt
        rechecked_asof = None
        if projection.error is None and selected is not None:
            capture_day = selected.received_at.astimezone(_JST).date()
            if on_date < capture_day:
                proof = read_list_responses(conn, doc_date=capture_day)
                rechecked_asof = document_list_rechecked_asof(projection, proof)
    finally:
        if connection is None:
            conn.rollback()
            conn.close()
    store_edinet_document_projection(
        sqlite_path, projection, rechecked_asof=rechecked_asof, connection=connection
    )
    return projection


def rebuild_edinet_lists_command(
    *,
    sqlite_path: Path,
    start: date,
    end: date,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Replay a complete retained date range into L2 after a read-only preflight."""
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    if start > end:
        print("--start must be on or before --end", file=errors)
        return 1
    if not sqlite_path.is_file():
        print(f"EDINET retained source database is missing: {sqlite_path}", file=errors)
        return 1
    days: list[date] = []
    current = start
    while True:
        days.append(current)
        if current == end:
            break
        current += timedelta(days=1)
    try:
        connection = connect_current(sqlite_path)
        if connection is None:
            raise EDINETProviderError("EDINET retained source database is unavailable")
        try:
            available = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT doc_date FROM edinet_list_responses "
                    "WHERE doc_date BETWEEN ? AND ?",
                    (start.isoformat(), end.isoformat()),
                )
            }
        finally:
            connection.close()
    except (sqlite3.Error, SQLiteSchemaError, EDINETProviderError) as exc:
        print(f"EDINET list preflight failed: {exc}", file=errors)
        return 1
    missing = [day.isoformat() for day in days if day.isoformat() not in available]
    if missing:
        print("missing raw EDINET list receipts: " + ", ".join(missing), file=errors)
        return 1

    succeeded = failed = projected_rows = 0
    for day in days:
        try:
            projection = rebuild_edinet_document_day(sqlite_path, day)
        except Exception as exc:
            failed += 1
            print(f"{day.isoformat()}: rebuild failed: {exc}", file=errors)
            continue
        if projection.error is not None:
            failed += 1
            print(f"{day.isoformat()}: raw projection failed: {projection.error}", file=errors)
        else:
            succeeded += 1
            projected_rows += len(projection.documents)
    print(
        f"EDINET list rebuild: days_succeeded={succeeded} days_failed={failed} "
        f"projected_rows={projected_rows}",
        file=output,
    )
    return int(failed != 0)


def store_edinet_document_projection(
    db_path: Path,
    projection: DocumentListProjection,
    *,
    rechecked_asof: date | None,
    connection: sqlite3.Connection | None = None,
) -> int:
    """Replace one day's derived rows and coverage from a raw-receipt projection."""
    conn = connection if connection is not None else open_connection(db_path)
    try:
        if connection is None:
            conn.execute("BEGIN IMMEDIATE")
        doc_date = projection.doc_date
        doc_date_iso = doc_date.isoformat()
        rows = document_rows(doc_date_iso, projection.documents)
        selected = projection.selected_receipt
        if selected is None:
            process_datetime = None
            result_count = 0
            response_json = None
            selected_received_at_iso = None
        else:
            payload = projection.native_payload
            if payload is None:
                raise ValueError("selected EDINET receipt has no native payload")
            metadata = payload["metadata"]
            process_datetime = document_text(metadata.get("processDateTime"))
            result_count = int(str(metadata["resultset"]["count"]))
            response_json = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            )
            selected_received_at_iso = selected.received_at.astimezone(UTC).isoformat(
                timespec="microseconds"
            )
        latest_at_iso = projection.latest_received_at.astimezone(UTC).isoformat(
            timespec="microseconds"
        )
        successful = projection.error is None and selected is not None
        conn.execute("DELETE FROM edinet_documents WHERE doc_date = ?", (doc_date_iso,))
        delete_overlapping_source_coverage(conn, "edinet_documents", doc_date, doc_date)
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
            conn, "edinet_documents", "doc_date", doc_date, doc_date
        )
        record_source_coverage(
            conn,
            source="edinet_documents",
            coverage_key=doc_date_iso,
            coverage_start=doc_date_iso,
            coverage_end=doc_date_iso,
            record_count=persisted_count,
            status="ok" if successful else "failed",
            error=None if successful else projection.error or "no selected EDINET receipt",
            fetched_at_utc=latest_at_iso,
        )
        conn.execute(
            "INSERT OR REPLACE INTO edinet_document_lists("
            "doc_date, process_datetime, result_count, selected_received_at_utc, "
            "response_json, selected_receipt_id, latest_received_at_utc, rechecked_asof"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc_date_iso,
                process_datetime,
                result_count,
                selected_received_at_iso,
                response_json,
                selected.receipt_id if selected is not None else None,
                latest_at_iso,
                rechecked_asof.isoformat() if successful and rechecked_asof is not None else None,
            ),
        )
        if connection is None:
            conn.commit()
        return persisted_count
    except BaseException:
        if connection is None:
            conn.rollback()
        raise
    finally:
        if connection is None:
            conn.close()
