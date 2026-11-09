"""Compose retained filing identity with the supplied keys of native operation rows."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import duckdb

from baibai_engine.data_collection.providers.edinet.document_events import (
    canonicalize_document_events,
)
from baibai_engine.data_collection.providers.edinet.document_rows import (
    is_document_operation,
    native_document_index,
)
from baibai_engine.data_collection.providers.edinet.models import EDINETProviderError
from baibai_engine.storage.sqlite.source_cache.coverage import date_covered
from baibai_engine.storage.sqlite.source_cache.schema import connect_current


def read_edinet_documents(sqlite_path: Path, on_date: date) -> list[dict[str, Any]] | None:
    """Read retained origins and source-key-preserving operations for one day.

    Missing coverage returns None; database query failures propagate.
    """
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        conn.execute("BEGIN")
        if not date_covered(conn, "edinet_documents", on_date):
            return None
        return read_stored_documents(conn, through=on_date, start=on_date)
    finally:
        conn.close()


def read_current_filing_documents(
    connection: sqlite3.Connection, *, through: date
) -> list[dict[str, Any]]:
    """Fold retained edits and withdrawals before interpreting filing metadata.

    Operations without a retained origin do not establish a filing. Raw operation
    rows remain available through read_stored_documents for extraction diagnostics.
    """
    filings, _ = canonicalize_document_events(
        read_stored_documents(connection, through=through), origin_start=None
    )
    return filings


def read_stored_documents(
    connection: sqlite3.Connection, *, through: date, start: date | None = None
) -> list[dict[str, Any]]:
    """Read stored fold inputs without turning omitted source keys into explicit nulls.

    Ordinary origins use the projection, which retains identities observed before
    inspection expiry. Native operation rows keep exactly their supplied keys.
    Normalized-only operation rows have no evidence of key presence: their null
    placeholders remain unknown sparse input, not a reconstructed response.
    """
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        start_iso = start.isoformat() if start else None
        failed = connection.execute(
            "SELECT coverage_start FROM source_coverage "
            "WHERE source = 'edinet_documents' AND status <> 'ok' "
            "AND coverage_start IS NOT NULL AND coverage_end IS NOT NULL "
            "AND coverage_start <= ? AND (? IS NULL OR coverage_end >= ?) "
            "ORDER BY coverage_start LIMIT 1",
            (through.isoformat(), start_iso, start_iso),
        ).fetchone()
        if failed is not None:
            failed_date = max(str(failed[0]), start_iso) if start_iso else str(failed[0])
            raise EDINETProviderError(f"EDINET document coverage failed: {failed_date}")
        return read_observed_documents(connection, through=through, start=start)
    finally:
        if owns_transaction:
            connection.rollback()


def read_observed_documents(
    connection: sqlite3.Connection | duckdb.DuckDBPyConnection,
    *,
    through: date,
    start: date | None = None,
    holder_only: bool = False,
) -> list[dict[str, Any]]:
    """Read retained evidence, without claiming completeness of acquisition days.

    Input eligibility uses known corrections/withdrawals even when a later list
    fetch failed. Native/projection inconsistencies still raise; callers own the
    read transaction. Extraction requiring complete lists uses read_stored_documents.
    """
    start_iso = start.isoformat() if start else None
    # When filtering filings, retain operation rows for the same document or
    # its correction children; filtering directly on a nullable type loses edits.
    scope = ""
    parameters: tuple[object, ...] = (through.isoformat(), start_iso, start_iso)
    if holder_only:
        scope = (
            " AND (d.doc_type_code IN ('350','360') OR d.doc_id IN ("
            "SELECT doc_id FROM edinet_documents WHERE doc_date<=? "
            "AND doc_type_code IN ('350','360'))"
            " OR d.parent_doc_id IN (SELECT doc_id FROM edinet_documents "
            "WHERE doc_date<=? AND doc_type_code IN ('350','360')))"
        )
        parameters += (through.isoformat(), through.isoformat())
    cursor = connection.execute(
        "SELECT d.doc_date, d.sequence_number AS seqNumber, d.doc_id AS docID, "  # nosec B608
        "d.sec_code AS secCode, d.doc_type_code AS docTypeCode, d.csv_flag AS csvFlag, "
        "d.xbrl_flag AS xbrlFlag, d.legal_status AS legalStatus, "
        "d.disclosure_status AS disclosureStatus, d.withdrawal_status AS withdrawalStatus, "
        "d.doc_info_edit_status AS docInfoEditStatus, d.parent_doc_id AS parentDocID, "
        "d.operation_datetime AS opeDateTime, d.submit_datetime AS submitDateTime, "
        "d.doc_description AS docDescription, d.period_start AS periodStart, "
        "d.period_end AS periodEnd, d.edinet_code AS edinetCode, "
        "d.issuer_edinet_code AS issuerEdinetCode, d.subject_edinet_code AS subjectEdinetCode "
        "FROM edinet_documents d "
        "WHERE d.doc_date <= ? AND (? IS NULL OR d.doc_date >= ?) "
        + scope
        + " ORDER BY d.doc_date, d.sequence_number",
        parameters,
    )
    assert cursor.description is not None
    columns = tuple(column[0] for column in cursor.description)
    current_date: str | None = None
    native: dict[tuple[int, str], dict[str, Any]] | None = None
    documents: list[dict[str, Any]] = []
    # DuckDB uses the connection as its cursor. Preserve this result before the
    # per-day native-list queries below, exactly as for the SQLite projection.
    for values in cursor.fetchall():
        projected = dict(zip(columns, values, strict=True))
        doc_date = str(projected["doc_date"])
        if doc_date != current_date:
            saved = connection.execute(
                "SELECT response_json FROM edinet_document_lists WHERE doc_date = ?",
                (doc_date,),
            ).fetchone()
            response = saved[0] if saved is not None else None
            native = (
                _native_documents(response, doc_date=doc_date) if response is not None else None
            )
            current_date = doc_date
        if native is None:
            documents.append(
                {key: value for key, value in projected.items() if value is not None}
                if is_document_operation(projected)
                else projected
            )
            continue
        identity = (int(projected["seqNumber"]), str(projected["docID"]))
        source = native.get(identity)
        if source is None:
            raise EDINETProviderError(
                f"EDINET stored document has no matching native row: {doc_date}/{identity}"
            )
        documents.append(
            {**source, "doc_date": doc_date} if is_document_operation(source) else projected
        )
    return documents


def _native_documents(response: str, *, doc_date: str) -> dict[tuple[int, str], dict[str, Any]]:
    try:
        payload = json.loads(response)
    except (TypeError, ValueError) as error:
        raise EDINETProviderError(f"EDINET saved response is invalid: {doc_date}") from error
    return native_document_index(payload, doc_date=doc_date)
