"""Materialize EDINET lists from retained source receipts without network access."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterable, Mapping
from datetime import UTC, date, timedelta
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo

from baibai_engine.data_collection.providers.edinet.receipts import read_list_responses
from baibai_engine.machine_analysis.observations.disclosures.edinet_list_projection import (
    DocumentListProjection,
    document_list_rechecked_asof,
    project_document_list,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.machine_analysis.observations.normalization import (
    date_iso,
    first,
    to_str_or_none,
)
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


def rebuild_edinet_document_day(sqlite_path: Path, on_date: date) -> DocumentListProjection:
    """Save current success or failed coverage, then return the deterministic result."""
    if not sqlite_path.exists():
        raise EDINETProviderError("EDINET retained source database is missing")
    connection = connect_current(sqlite_path)
    if connection is None:
        raise EDINETProviderError("EDINET retained source database is unavailable")
    try:
        connection.execute("BEGIN")
        receipts = read_list_responses(connection, doc_date=on_date)
        if not receipts:
            raise EDINETProviderError(f"EDINET list receipts are missing: {on_date.isoformat()}")
        projection = project_document_list(receipts)
        selected = projection.selected_receipt
        rechecked_asof = None
        if projection.error is None and selected is not None:
            capture_day = selected.received_at.astimezone(_JST).date()
            if on_date < capture_day:
                proof = read_list_responses(connection, doc_date=capture_day)
                rechecked_asof = document_list_rechecked_asof(projection, proof)
    finally:
        connection.rollback()
        connection.close()
    store_edinet_document_projection(sqlite_path, projection, rechecked_asof=rechecked_asof)
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
    db_path: Path, projection: DocumentListProjection, *, rechecked_asof: date | None
) -> int:
    """Replace one day's derived rows and coverage from a raw-receipt projection."""
    conn = open_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        doc_date = projection.doc_date
        doc_date_iso = doc_date.isoformat()
        rows = _edinet_document_rows(doc_date_iso, projection.documents)
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
            process_datetime = to_str_or_none(metadata.get("processDateTime"))
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
        conn.commit()
        return persisted_count
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _edinet_document_rows(
    doc_date: str,
    records: Iterable[Mapping[str, Any]],
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    sequence_numbers: set[int] = set()
    for record in records:
        doc_id = to_str_or_none(first(record, "docID", "doc_id"))
        if doc_id is None:
            raise ValueError("EDINET document row is missing docID")
        raw_sequence = first(record, "seqNumber", "sequence_number")
        if raw_sequence in (None, ""):
            raise ValueError("EDINET document row is missing seqNumber")
        if isinstance(raw_sequence, bool):
            raise ValueError(f"invalid EDINET seqNumber: {raw_sequence!r}")
        if isinstance(raw_sequence, int):
            sequence_number = raw_sequence
        elif isinstance(raw_sequence, str) and raw_sequence.isdecimal():
            sequence_number = int(raw_sequence)
        else:
            raise ValueError(f"invalid EDINET seqNumber: {raw_sequence!r}")
        if sequence_number <= 0:
            raise ValueError(f"invalid EDINET seqNumber: {raw_sequence!r}")
        if sequence_number in sequence_numbers:
            raise ValueError(f"duplicate EDINET seqNumber: {sequence_number}")
        sequence_numbers.add(sequence_number)
        retained_values = {
            "sec_code": to_str_or_none(first(record, "secCode", "sec_code")),
            "doc_type_code": to_str_or_none(first(record, "docTypeCode", "doc_type_code")),
            "parent_doc_id": to_str_or_none(first(record, "parentDocID", "parent_doc_id")),
            "submit_datetime": to_str_or_none(first(record, "submitDateTime", "submit_datetime")),
            "doc_description": to_str_or_none(first(record, "docDescription", "doc_description")),
            "edinet_code": to_str_or_none(first(record, "edinetCode", "edinet_code")),
            "issuer_edinet_code": to_str_or_none(
                first(record, "issuerEdinetCode", "issuer_edinet_code")
            ),
            "subject_edinet_code": to_str_or_none(
                first(record, "subjectEdinetCode", "subject_edinet_code")
            ),
        }
        rows.append(
            (
                doc_date,
                sequence_number,
                doc_id,
                retained_values["sec_code"],
                retained_values["doc_type_code"],
                to_str_or_none(first(record, "csvFlag", "csv_flag")),
                to_str_or_none(first(record, "xbrlFlag", "xbrl_flag")),
                to_str_or_none(first(record, "legalStatus", "legal_status")),
                to_str_or_none(first(record, "disclosureStatus", "disclosure_status")),
                to_str_or_none(first(record, "withdrawalStatus", "withdrawal_status")),
                to_str_or_none(first(record, "docInfoEditStatus", "doc_info_edit_status")),
                retained_values["parent_doc_id"],
                to_str_or_none(first(record, "opeDateTime", "operation_datetime")),
                retained_values["submit_datetime"],
                retained_values["doc_description"],
                date_iso(first(record, "periodStart", "period_start")),
                date_iso(first(record, "periodEnd", "period_end")),
                retained_values["edinet_code"],
                retained_values["issuer_edinet_code"],
                retained_values["subject_edinet_code"],
            )
        )
    return rows
