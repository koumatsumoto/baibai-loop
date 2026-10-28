"""Offline readers of deterministically processed JPX snapshots."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from baibai_engine.machine_analysis.observations.disclosures.jpx_models import (
    JPXEarningsCalendarEntry,
    JPXEarningsCalendarSnapshot,
    JPXProviderError,
    JPXRegulationSnapshot,
    index_calendar_announcements,
    next_earnings_dates,
)
from baibai_engine.primitives.dates import weekday_distance
from baibai_engine.primitives.time import JST
from baibai_engine.storage.sqlite.read import is_unwritten_database
from baibai_engine.storage.sqlite.source_cache.coverage import date_covered
from baibai_engine.storage.sqlite.source_cache.schema import connect_current


def read_next_earnings_dates(sqlite_path: Path, asof_date: date) -> dict[str, date] | None:
    """Read verified current dates; None means unavailable, not a published empty set.

    Coverage identity, source freshness and snapshot integrity belong to the stored
    snapshot reader. Schema/query failures and corrupt snapshots propagate. This
    optional read never acquires data or makes unrelated market sources mandatory.
    """
    snapshot = read_jpx_earnings_calendar_snapshot(sqlite_path, asof_date)
    return None if snapshot is None else next_earnings_dates(snapshot.entries, asof_date)


class JPXReader:
    def __init__(self, sqlite_path: Path, *, allow_stale_snapshot: bool = False) -> None:
        self._sqlite_path = sqlite_path
        self._allow_stale_snapshot = allow_stale_snapshot

    def get_regulation_snapshot(self, asof_date: date) -> JPXRegulationSnapshot:
        snapshot = read_jpx_regulations(self._sqlite_path, asof_date)
        if snapshot is None:
            raise JPXProviderError(
                f"stored JPX regulation snapshot is unavailable for {asof_date.isoformat()}; "
                "acquire and project the source explicitly before reading"
            )
        return snapshot

    def get_earnings_calendar_snapshot(self, asof_date: date) -> JPXEarningsCalendarSnapshot:
        snapshot = read_jpx_earnings_calendar_snapshot(
            self._sqlite_path, asof_date, allow_stale=self._allow_stale_snapshot
        )
        if snapshot is None:
            raise JPXProviderError(
                f"stored JPX earnings calendar is unavailable for {asof_date.isoformat()}; "
                "acquire and project the source explicitly before reading"
            )
        return snapshot


def read_jpx_earnings_calendar_snapshot(
    sqlite_path: Path,
    asof_date: date,
    *,
    allow_stale: bool = False,
) -> JPXEarningsCalendarSnapshot | None:
    """Read current JPX coverage; unavailable returns None, corrupt success raises."""
    if not sqlite_path.exists() or is_unwritten_database(sqlite_path):
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        conn.execute("BEGIN")
        coverage = conn.execute(
            "SELECT coverage_start, coverage_end, fetched_at_utc, "
            "record_count, status FROM source_coverage "
            "WHERE source = 'jpx_earnings_calendar' "
            "AND coverage_key = 'get_earnings_calendar_snapshot:current'"
        ).fetchone()
        if coverage is None:
            return None
        coverage_start, coverage_end, fetched_at_text, record_count, status = coverage
        if status != "ok":
            return None
        try:
            recorded_start = date.fromisoformat(str(coverage_start))
            recorded_end = date.fromisoformat(str(coverage_end))
            fetched_at = datetime.fromisoformat(str(fetched_at_text).replace("Z", "+00:00"))
            expected_count = int(record_count)
        except (TypeError, ValueError) as error:
            raise JPXProviderError(f"corrupt JPX earnings calendar coverage: {error}") from error
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise JPXProviderError(
                "corrupt JPX earnings calendar coverage: fetched_at lacks timezone"
            )
        rows = conn.execute(
            "SELECT ticker, announcement_date FROM jpx_earnings_calendar "
            "ORDER BY announcement_date, ticker"
        ).fetchall()
        if not rows or len(rows) != expected_count:
            raise JPXProviderError("corrupt JPX earnings calendar snapshot: row count mismatch")
        try:
            entries = tuple(
                JPXEarningsCalendarEntry(
                    ticker=str(ticker), announcement_date=date.fromisoformat(str(announcement_date))
                )
                for ticker, announcement_date in rows
            )
            index_calendar_announcements(entries)
        except (TypeError, ValueError) as error:
            raise JPXProviderError(f"corrupt JPX earnings calendar snapshot: {error}") from error
        snapshot = JPXEarningsCalendarSnapshot(
            entries=entries,
            source_urls=(),
            raw_record_count=len(entries),
            excluded_record_count=0,
            superseded_record_count=None,
        )
        if snapshot.min_date != recorded_start or snapshot.max_date != recorded_end:
            raise JPXProviderError("corrupt JPX earnings calendar snapshot: date range mismatch")
        if snapshot.max_date < asof_date:
            return None
        if not allow_stale and weekday_distance(asof_date, fetched_at.astimezone(JST).date()) > 7:
            return None
        return snapshot
    finally:
        conn.close()


def read_jpx_regulations(sqlite_path: Path, asof_date: date) -> JPXRegulationSnapshot | None:
    """Return the JPX regulation snapshot for `asof_date`, or `None` if the
    cache has not imported a snapshot for that date.
    """
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        conn.execute("BEGIN")
        if not date_covered(conn, "jpx_regulation_flags", asof_date):
            return None
        rows = conn.execute(
            "SELECT source_name, ticker, flag FROM jpx_regulation_flags "
            "WHERE asof_date = ? ORDER BY ticker, flag",
            (asof_date.isoformat(),),
        ).fetchall()
        source_rows = conn.execute(
            "SELECT source_name FROM jpx_regulation_sources "
            "WHERE asof_date = ? ORDER BY source_name",
            (asof_date.isoformat(),),
        ).fetchall()
    finally:
        conn.close()

    flags: dict[str, list[str]] = {}
    source_names: set[str] = {str(source_name) for (source_name,) in source_rows}
    for source_name, ticker, flag in rows:
        flags.setdefault(ticker, []).append(flag)
        source_names.add(source_name)
    return JPXRegulationSnapshot(
        flags_by_ticker={ticker: tuple(values) for ticker, values in flags.items()},
        source_names=tuple(sorted(source_names)),
    )


def has_jpx_regulation_data(sqlite_path: Path, asof_date: date) -> bool:
    """Return True when `asof_date` has an imported JPX regulation snapshot.

    A valid snapshot may have zero flagged tickers for a source such as
    取引停止. Treat `source_coverage` as the canonical cache coverage marker so
    stale backfill gating does not force a refetch just because a required
    source returned an empty source-specific table on that date.
    """
    if not sqlite_path.exists():
        return False
    conn = connect_current(sqlite_path)
    if conn is None:
        return False
    try:
        return date_covered(conn, "jpx_regulation_flags", asof_date)
    finally:
        conn.close()
