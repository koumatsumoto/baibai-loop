"""保存済み観測の責務別読取。取得・公開は行わない。"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from baibai_engine.machine_analysis.observations.financials.financial_documents import (
    disclosure_revision,
    normalize_financial_summary,
    project_financial_documents,
)
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.storage.sqlite.source_cache.coverage import range_covered
from baibai_engine.storage.sqlite.source_cache.schema import connect_current


def read_fin_summaries(
    sqlite_path: Path, start: date, end: date
) -> list[JQuantsFinancialSummary] | None:
    """Read covered source documents; project forecasts under the same pure contract."""
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        conn.execute("BEGIN")
        if not range_covered(conn, "jquants_fin_summaries", start, end):
            return None
        documents = read_financial_documents(conn, start=start, end=end)
        # Projection groups by ticker before its stable disclosure-time sort.
        documents.sort(key=lambda row: (row.ticker, row.disclosed_at, row.disclosure_number or ""))
        return project_financial_documents(documents, end)
    finally:
        conn.close()


def read_financial_documents(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
) -> list[JQuantsFinancialSummary]:
    """Decode stored documents in disclosure order, without coverage or forecast projection."""
    cursor = conn.execute(
        "SELECT document_payload FROM jquants_fin_summaries "
        "WHERE disclosed_at BETWEEN ? AND ? "
        "ORDER BY ticker, disclosed_at, disclosure_number",
        (start.isoformat(), end.isoformat()),
    )
    summaries: list[JQuantsFinancialSummary] = []
    while batch := cursor.fetchmany(256):
        for (payload,) in batch:
            try:
                record = json.loads(payload)
                if not isinstance(record, dict):
                    raise ValueError("financial document must be a provider record")
                summary = normalize_financial_summary(record)
                if summary is None:
                    raise ValueError("financial document is not an eligible security")
                summaries.append(summary)
            except (TypeError, ValueError) as exc:
                raise JQuantsProviderError(f"corrupt financial document: {exc}") from exc
    # Preserve the former projection's stable order, including same-time document ties.
    summaries.sort(key=disclosure_revision)
    return summaries
