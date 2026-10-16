"""Compose retained filing identity with the supplied keys of native operation rows."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import date
from typing import Any

from baibai_engine.market.edinet_models import EDINETProviderError


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
        return _read_stored_documents(connection, through=through, start=start)
    finally:
        if owns_transaction:
            connection.rollback()


def _read_stored_documents(
    connection: sqlite3.Connection, *, through: date, start: date | None
) -> list[dict[str, Any]]:
    start_iso = start.isoformat() if start else None
    cursor = connection.execute(
        "SELECT d.doc_date, d.sequence_number AS seqNumber, d.doc_id AS docID, "
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
        "ORDER BY d.doc_date, d.sequence_number",
        (through.isoformat(), start_iso, start_iso),
    )
    columns = tuple(column[0] for column in cursor.description)
    current_date: str | None = None
    native: dict[tuple[int, str], dict[str, Any]] | None = None
    documents: list[dict[str, Any]] = []
    for values in cursor:
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
                if _is_operation(projected)
                else projected
            )
            continue
        identity = (int(projected["seqNumber"]), str(projected["docID"]))
        source = native.get(identity)
        if source is None:
            raise EDINETProviderError(
                f"EDINET stored document has no matching native row: {doc_date}/{identity}"
            )
        documents.append({**source, "doc_date": doc_date} if _is_operation(source) else projected)
    return documents


def _is_operation(document: Mapping[str, Any]) -> bool:
    return (
        str(document.get("docInfoEditStatus")) == "1"
        or str(document.get("withdrawalStatus")) == "1"
        or str(document.get("disclosureStatus")) in {"1", "3"}
    )


def _native_documents(response: str, *, doc_date: str) -> dict[tuple[int, str], dict[str, Any]]:
    try:
        payload = json.loads(response)
    except (TypeError, ValueError) as error:
        raise EDINETProviderError(f"EDINET saved response is invalid: {doc_date}") from error
    results = payload.get("results") if isinstance(payload, Mapping) else None
    if not isinstance(results, list):
        raise EDINETProviderError(f"EDINET saved response has no results list: {doc_date}")
    documents: dict[tuple[int, str], dict[str, Any]] = {}
    for source in results:
        if not isinstance(source, Mapping):
            raise EDINETProviderError(f"EDINET saved response has an invalid row: {doc_date}")
        sequence = source.get("seqNumber")
        doc_id = source.get("docID")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, (int, str))
            or not str(sequence).isdecimal()
            or int(sequence) <= 0
            or not isinstance(doc_id, str)
            or not doc_id
        ):
            raise EDINETProviderError(f"EDINET saved row has an invalid identity: {doc_date}")
        identity = (int(sequence), doc_id)
        if identity in documents:
            raise EDINETProviderError(f"EDINET saved response repeats a row: {doc_date}/{identity}")
        documents[identity] = dict(source)
    return documents
