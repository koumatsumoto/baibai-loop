"""Store filing facts atomically; extraction coverage uses the existing bookkeeping."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from baibai_engine.machine_analysis.observations.disclosures.edinet_document_store import read_stored_documents
from baibai_engine.storage.sqlite.source_cache.coverage import record_source_coverage
from baibai_engine.storage.sqlite.source_cache.schema import connect_current, open_connection

from baibai_engine.machine_analysis.observations.financials.edinet_facts.extract import EXTRACTOR_REVISION
from baibai_engine.machine_analysis.observations.financials.edinet_facts.models import DebtFact, ExtractedFacts, FactIdentity, SegmentFact

COVERAGE_SOURCE = "edinet_research_facts"


def document_inventory(path: Path, through: date) -> list[dict[str, Any]]:
    connection = connect_current(path)
    if connection is None:
        raise ValueError("EDINET document store is unavailable")
    try:
        return read_stored_documents(connection, through=through)
    finally:
        connection.close()


def extraction_status(path: Path) -> dict[str, tuple[str, str | None]]:
    connection = connect_current(path)
    if connection is None:
        raise ValueError("market store is unavailable")
    try:
        return {
            str(doc): (str(status), str(error) if error else None)
            for doc, status, error in connection.execute(
                "SELECT coverage_key, status, error FROM source_coverage WHERE source = ?",
                (COVERAGE_SOURCE,),
            )
        }
    finally:
        connection.close()


def store_facts(path: Path, *, identity: FactIdentity, facts: ExtractedFacts) -> None:
    doc_id = identity.source_doc_id
    disclosed_on = identity.disclosed_on
    connection = open_connection(path)
    try:
        with connection:
            _write_rows(connection, "edinet_segment_facts", facts.segments, doc_id)
            _write_rows(connection, "edinet_debt_schedule", facts.debt, doc_id)
            record_source_coverage(
                connection,
                source=COVERAGE_SOURCE,
                coverage_key=doc_id,
                coverage_start=disclosed_on,
                coverage_end=disclosed_on,
                record_count=len(facts.segments) + len(facts.debt),
                status="ok",
                error=json.dumps(
                    {
                        "revision": EXTRACTOR_REVISION,
                        "identity": identity.model_dump(),
                        "segment_reasons": facts.segment_reasons,
                        "debt_reasons": facts.debt_reasons,
                    },
                    sort_keys=True,
                ),
            )
    finally:
        connection.close()


def record_failure(path: Path, *, doc_id: str, disclosed_on: str, reason: str) -> None:
    connection = open_connection(path)
    try:
        with connection:
            record_source_coverage(
                connection,
                source=COVERAGE_SOURCE,
                coverage_key=doc_id,
                coverage_start=disclosed_on,
                coverage_end=disclosed_on,
                record_count=0,
                status="failed",
                error=reason,
            )
    finally:
        connection.close()


def record_initialized(path: Path, asof: date) -> None:
    connection = open_connection(path)
    try:
        with connection:
            record_source_coverage(
                connection,
                source=COVERAGE_SOURCE,
                coverage_key="initialized",
                coverage_start=asof.isoformat(),
                coverage_end=asof.isoformat(),
                record_count=0,
                status="ok",
                error=json.dumps({"since": asof.isoformat()}),
            )
    finally:
        connection.close()


def _write_rows(
    connection: sqlite3.Connection,
    table: str,
    rows: Sequence[SegmentFact] | Sequence[DebtFact],
    doc_id: str,
) -> None:
    # table/columns come exclusively from these two internal typed models, never from
    # the provider. Replacing this document cannot overwrite another filing's vintage.
    connection.execute(f"DELETE FROM {table} WHERE source_doc_id = ?", (doc_id,))  # nosec B608
    if not rows:
        return
    columns = tuple(type(rows[0]).model_fields)
    query = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})"  # nosec B608
    connection.executemany(query, [tuple(row.model_dump()[key] for key in columns) for row in rows])
