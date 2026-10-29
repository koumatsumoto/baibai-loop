"""Recent revision checks and outage recovery for economic source receipts.

Keep the existing windows until a source's publication/revision contract justifies
a change. A recent check is not a history rebuild or a bound on outage recovery.
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from baibai_engine.data_collection.catalog import SourceDefinition, load_sources
from baibai_engine.data_collection.receipts.native_sources.documents import read_documents

DEFAULT_LATEST_LOOKBACK_DAYS = 370
LATEST_FETCH_LOOKBACK_DAYS = {"daily": 14, "weekly": 60, "monthly": 370, "quarterly": 370}

# Providers sharing credentials/rate limits stay in one sequential acquisition.
_GROUPS = {
    "boj_timeseries": "boj",
    "estat_dashboard": "estat",
    "jquants_options": "jquants",
    "jquants_flows": "jquants",
    "jquants_indices": "jquants",
}
_BULK = frozenset(
    {"boj", "ecb_fx", "fred_csv", "estat", "estat_dashboard", "mof_jgb", "umich_sca", "multpl"}
)


@dataclass(frozen=True, slots=True)
class RefreshPolicy:
    acquisition_group: str
    mode: str
    lookback_days: int


def source_policy(source: SourceDefinition) -> RefreshPolicy:
    mode = "range"
    if source.provider in _BULK:
        mode = "bulk_then_filter"
    elif source.provider == "matsui_margin":
        mode = "current_only"
    elif source.provider in {"spglobal_pmi", "tradingeconomics_pmi", "tsr_bankruptcies"}:
        mode = "documents"
    elif source.provider == "jquants_options":
        mode = "day_requests"
    return RefreshPolicy(
        acquisition_group=_GROUPS.get(source.provider, source.provider),
        mode=mode,
        lookback_days=LATEST_FETCH_LOOKBACK_DAYS.get(
            source.frequency, DEFAULT_LATEST_LOOKBACK_DAYS
        ),
    )


def acquisition_groups() -> tuple[str, ...]:
    return tuple(
        sorted({source_policy(source).acquisition_group for source in load_sources().values()})
    )


def recent_start(source: SourceDefinition, end: date) -> date:
    """The revision-check window never implicitly resumes a failed history rebuild."""
    policy = source_policy(source)
    if policy.mode == "current_only":
        return end
    return end - timedelta(days=policy.lookback_days)


def recovery_ranges(
    connection: sqlite3.Connection, source: SourceDefinition, end: date
) -> tuple[tuple[date, date], ...]:
    """Recover unreceived intervals in the established ongoing collection range.

    A successful range establishes coverage and an ongoing collection anchor. A
    failed ordinary-size range can anchor an initial outage too. A failed broad
    backfill never extends that anchor; its failure remains recorded for explicit
    recovery. Individual committed native days/PDF months cover partial successes,
    so a failed range does not imply that all its originals are missing.
    """
    if source_policy(source).mode == "current_only":
        return ()
    recent = recent_start(source, end)
    last = recent - timedelta(days=1)
    rows = connection.execute(
        "SELECT range_start, range_end, status FROM economic_source_runs "
        "WHERE source_id = ? AND range_start <= ? ORDER BY range_start, range_end",
        (source.source_id, end.isoformat()),
    ).fetchall()
    anchors: list[date] = []
    covered: list[tuple[date, date]] = []
    for start_raw, end_raw, status in rows:
        start, finish = date.fromisoformat(start_raw), date.fromisoformat(end_raw)
        if status == "ok":
            anchors.append(start)
            if source.provider not in {"spglobal_pmi", "tradingeconomics_pmi"}:
                covered.append((start, finish))
        elif (finish - start).days <= source_policy(source).lookback_days:
            anchors.append(start)
    if not anchors or min(anchors) > last:
        return ()
    first = min(anchors)
    return _missing_ranges(connection, source, first, last, covered)


def explicit_recovery_ranges(
    connection: sqlite3.Connection, source: SourceDefinition, end: date
) -> tuple[tuple[date, date], ...]:
    """Expose unfinished broad acquisitions without restarting them in a daily run."""
    pending: list[tuple[date, date]] = []
    anchor_row = connection.execute(
        "SELECT MIN(range_start) FROM economic_source_runs WHERE source_id = ? "
        "AND (status = 'ok' OR julianday(range_end) - julianday(range_start) <= ?)",
        (source.source_id, source_policy(source).lookback_days),
    ).fetchone()
    anchor = date.fromisoformat(anchor_row[0]) if anchor_row and anchor_row[0] else None
    for start_raw, end_raw in connection.execute(
        "SELECT DISTINCT range_start, range_end FROM economic_source_runs "
        "WHERE source_id = ? AND status = 'failed' AND range_start <= ?",
        (source.source_id, end.isoformat()),
    ):
        first, finish = date.fromisoformat(start_raw), date.fromisoformat(end_raw)
        if (finish - first).days <= source_policy(source).lookback_days:
            continue
        last = min(end, finish)
        if anchor is not None:
            last = min(last, anchor - timedelta(days=1))
        if first <= last:
            pending.extend(_missing_ranges(connection, source, first, last, []))
    return tuple(sorted(set(pending)))


def _missing_ranges(
    connection: sqlite3.Connection,
    source: SourceDefinition,
    first: date,
    last: date,
    covered: list[tuple[date, date]],
) -> tuple[tuple[date, date], ...]:
    successful_ranges = (
        (date.fromisoformat(row[0]), date.fromisoformat(row[1]))
        for row in connection.execute(
            "SELECT range_start, range_end FROM economic_source_runs "
            "WHERE source_id = ? AND status = 'ok' AND range_start <= ? AND range_end >= ?",
            (source.source_id, last.isoformat(), first.isoformat()),
        )
    )
    if source.provider not in {"spglobal_pmi", "tradingeconomics_pmi"}:
        covered.extend(successful_ranges)
    for (observed,) in connection.execute(
        "SELECT DISTINCT observed_at FROM economic_source_records "
        "WHERE source_id = ? AND observed_at BETWEEN ? AND ?",
        (source.source_id, first.isoformat(), last.isoformat()),
    ):
        day = date.fromisoformat(observed)
        covered.append((day, day))
    if source.provider in {"spglobal_pmi", "tradingeconomics_pmi"}:
        for document in read_documents(connection, source.source_id):
            identity = document.metadata
            if "observed_at" not in identity or identity.get("phase", "final") != "final":
                continue
            month = date.fromisoformat(str(identity["observed_at"]))
            covered.append(
                (month, month.replace(day=calendar.monthrange(month.year, month.month)[1]))
            )
    cursor = first
    missing: list[tuple[date, date]] = []
    for start, finish in sorted(covered):
        if finish < cursor:
            continue
        if start > last:
            break
        if start > cursor:
            missing.append((cursor, min(last, start - timedelta(days=1))))
        cursor = max(cursor, finish + timedelta(days=1))
        if cursor > last:
            break
    if cursor <= last:
        missing.append((cursor, last))
    return tuple(missing)
