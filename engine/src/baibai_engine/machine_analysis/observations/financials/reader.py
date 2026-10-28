"""保存済み観測の責務別読取。取得・公開は行わない。"""

from __future__ import annotations
import json
import sqlite3
from datetime import date
from pathlib import Path
from baibai_engine.machine_analysis.observations.financials.financial_documents import normalize_financial_summary, project_financial_documents
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.storage.observations.coverage import covered_intervals, range_covered
from baibai_engine.storage.observations.schema import connect_current
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import duckdb


def fin_summaries_covered(sqlite_path: Path, start: date, end: date) -> bool:
    """Answer whether the cache can serve `[start, end]` without building the rows.

    Deliberately the same predicate `read_fin_summaries` gates on, asked without
    the materialisation: a chunk-skip test and a fetch planner need the boolean,
    not the summaries. Sharing `range_covered` is what keeps them agreeing with
    the reader. A successful empty financial window is readable as an empty list,
    whereas an unexamined or partial window still needs a fetch. Required history
    and usable financial inputs are checked separately by the analysis consumers.
    """
    if not sqlite_path.exists():
        return False
    conn = connect_current(sqlite_path)
    if conn is None:
        return False
    try:
        return range_covered(conn, "jquants_fin_summaries", start, end)
    finally:
        conn.close()


def fin_summaries_readable_from(sqlite_path: Path, asof: date) -> date | None:
    """Where the store's **continuous** history ending at `asof` begins.

    The rows the store holds and the range it may serve are two different facts, and
    the subscription window moves. A filing fetched while the window still reached
    that far back keeps its row after the window passes it, so the oldest row can sit
    outside the coverage. A caller that takes its floor from the oldest row then asks
    for a range `read_fin_summaries` refuses, and a handful of rows at the far edge of
    the history takes every read down.

    Answering only for a continuous history is what keeps that relief from covering a
    real hole. A store that simply does not reach further back has one window and
    gets its floor; a store missing months in the middle has an older window too, and
    gets None so the caller keeps its own floor and the read reports the range it
    cannot serve. Silently starting after a gap would turn an outage into a quietly
    shorter history, which no diagnostic distinguishes from a young store.

    None also covers "no window holds `asof`" -- the same outage seen from the other
    end.
    """
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        intervals = covered_intervals(conn, "jquants_fin_summaries")
    finally:
        conn.close()
    for index, (interval_start, interval_end) in enumerate(intervals):
        if interval_start <= asof <= interval_end:
            return interval_start if index == 0 else None
    return None


def count_fin_summaries(sqlite_path: Path, start: date, end: date) -> int:
    """Count stored summary rows in `[start, end]`; 0 when the store cannot be read."""
    if not sqlite_path.exists():
        return 0
    conn = connect_current(sqlite_path)
    if conn is None:
        return 0
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM jquants_fin_summaries WHERE disclosed_at BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0] or 0) if row is not None else 0


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
        return read_financial_documents(conn, start=start, end=end)
    finally:
        conn.close()


def read_financial_documents(
    conn: sqlite3.Connection | duckdb.DuckDBPyConnection,
    *,
    start: date,
    end: date,
    ticker: str | None = None,
) -> list[JQuantsFinancialSummary]:
    """Read stored documents without asserting provider acquisition coverage."""
    cursor = conn.execute(
        "SELECT document_payload FROM jquants_fin_summaries "
        "WHERE disclosed_at BETWEEN ? AND ? AND (? IS NULL OR ticker = ?) "
        "ORDER BY ticker, disclosed_at, disclosure_number",
        (start.isoformat(), end.isoformat(), ticker, ticker),
    )
    summaries: list[JQuantsFinancialSummary] = []
    for (payload,) in cursor.fetchall():
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
    return project_financial_documents(summaries, end)
