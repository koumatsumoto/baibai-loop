"""Query-only operational state of the machine stores.

These reads answer "is the pipeline healthy", not "what should I buy". They are
kept apart from :mod:`.freshness`, whose values date the data a judgment view
shows; nothing here changes how a candidate, a reading, or a holding is read.

Two questions the judgment views cannot answer live here. A store's own size and
row count expose a retention or backfill accident that an unchanged as-of date
hides. And a provider that went silent is only visible as a *streak*: the latest
run per series already rides along with the macro reading, but "failing since
when, and for how many attempts" needs the run history behind it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from baibai_engine.macro.indicators.definitions import load_definitions

from .sqlite import connect_read_only

# One representative table per store: the one whose row count moves when the
# store's daily job works and collapses when retention misfires.
_STORE_TABLES = {
    "market": ("jquants_daily_bars", "traded_at"),
    "runs": ("screening_run", "asof_date"),
    "macro": ("observations", "observed_at"),
}


@dataclass(frozen=True, slots=True)
class StoreStats:
    """Size and depth of one machine store."""

    store: str
    exists: bool
    size_bytes: int | None
    row_count: int | None
    latest_date: date | None


def store_stats(store: str, path: Path) -> StoreStats:
    """Read one store's file size, representative row count, and newest data date.

    A store the checkout does not carry is a normal state (the application DB is
    the only one tracked in git), so absence reports as ``exists=False`` rather
    than raising.

    Depth is best-effort. This is an observability read on the publish path, and
    the whole export is fatal, so a renamed table or an unreadable store must
    degrade to "unknown depth" instead of withholding the dashboard, screening,
    and macro views that carry the actual judgment inputs.
    """

    if store not in _STORE_TABLES:
        raise ValueError(f"unknown store: {store}")
    if not path.is_file():
        return StoreStats(
            store=store, exists=False, size_bytes=None, row_count=None, latest_date=None
        )
    table, date_column = _STORE_TABLES[store]
    size_bytes = path.stat().st_size
    try:
        connection = connect_read_only(path)
        try:
            # Table and column come from the fixed mapping above, never from a caller.
            query = f"SELECT count(*), max({date_column}) FROM {table}"  # nosec B608
            row = connection.execute(query).fetchone()
        finally:
            connection.close()
        latest = None if row[1] is None else date.fromisoformat(str(row[1])[:10])
    except (sqlite3.Error, ValueError):
        # sqlite3.Error: renamed table, unreadable or non-SQLite file.
        # ValueError: a date column holding something that is not a date.
        return StoreStats(
            store=store, exists=True, size_bytes=size_bytes, row_count=None, latest_date=None
        )
    return StoreStats(
        store=store,
        exists=True,
        size_bytes=size_bytes,
        row_count=int(row[0]),
        latest_date=latest,
    )


def application_store_stats(path: Path) -> StoreStats:
    """Read the application DB's size; it holds judgment records, not a daily feed.

    No row count or data date is reported: the judgment store has no single table
    whose depth means anything operationally, and its freshness is already
    ``application_db_updated_at``.
    """

    if not path.is_file():
        return StoreStats(
            store="baibai", exists=False, size_bytes=None, row_count=None, latest_date=None
        )
    return StoreStats(
        store="baibai",
        exists=True,
        size_bytes=path.stat().st_size,
        row_count=None,
        latest_date=None,
    )


@dataclass(frozen=True, slots=True)
class ProviderFailureStreak:
    """How long one series has been failing, counted back from its latest run."""

    series_id: str
    consecutive_failures: int
    failing_since: str


def provider_failure_streaks(path: Path) -> list[ProviderFailureStreak]:
    """Return the current failure streak per registered series, worst first.

    A series appears only while its most recent run failed; one successful run
    ends the streak and drops it from the result. ``failing_since`` is the finish
    time of the oldest run in the unbroken run of failures, which is the moment a
    provider stopped answering — the value a staleness threshold cannot give for
    a low-frequency series until weeks later.

    Retired series are filtered out: the registry decides what is currently
    fetched, and history for a removed series is not an open problem.

    Like :func:`store_stats` this is best-effort: an unreadable store reports no
    outage rather than failing the export that publishes the judgment views.
    """

    if not path.is_file():
        return []
    registered = {series.series_id for series in load_definitions().series}
    if not registered:
        return []
    try:
        connection = connect_read_only(path)
        try:
            rows = connection.execute(
                # rowid, not run_id, breaks a finished_at tie: run ids are random
                # uuids, so ordering by them would pick a "newest" run by
                # lexicographic accident. rowid is insertion order.
                """
                SELECT series_id, status, finished_at FROM provider_runs
                ORDER BY series_id, finished_at DESC, rowid DESC
                """
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return []

    streaks: list[ProviderFailureStreak] = []
    current_series: str | None = None
    count = 0
    oldest_failure = ""
    broken = False
    for series_id, status, finished_at in ((str(r[0]), str(r[1]), str(r[2])) for r in rows):
        if series_id != current_series:
            _append_streak(streaks, current_series, count, oldest_failure)
            current_series = series_id
            count = 0
            oldest_failure = ""
            broken = False
        if broken or series_id not in registered:
            continue
        if status == "ok":
            # Rows arrive newest-first, so the first success closes the streak.
            broken = True
            continue
        count += 1
        oldest_failure = finished_at
    _append_streak(streaks, current_series, count, oldest_failure)
    streaks.sort(key=lambda streak: (-streak.consecutive_failures, streak.series_id))
    return streaks


def _append_streak(
    streaks: list[ProviderFailureStreak],
    series_id: str | None,
    count: int,
    oldest_failure: str,
) -> None:
    if series_id is not None and count > 0:
        streaks.append(
            ProviderFailureStreak(
                series_id=series_id,
                consecutive_failures=count,
                failing_since=oldest_failure,
            )
        )


def never_attempted_series(path: Path) -> list[str]:
    """Return registered series with no acquisition attempt on record at all.

    A streak needs rows to count; a series that was never tried has none, so it
    would otherwise be indistinguishable from a healthy one. The daily batch
    refreshes series in groups through one CLI call, so a call that dies partway
    leaves every remaining series in that group without a run record.

    An absent store reports nothing rather than every series: its own row already
    says the store is missing, and listing 119 "outages" on top would bury that.
    """

    registered = {series.series_id for series in load_definitions().series}
    if not registered or not path.is_file():
        return []
    try:
        connection = connect_read_only(path)
        try:
            rows = connection.execute("SELECT DISTINCT series_id FROM provider_runs").fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return []
    return sorted(registered - {str(row[0]) for row in rows})


__all__ = [
    "ProviderFailureStreak",
    "StoreStats",
    "application_store_stats",
    "never_attempted_series",
    "provider_failure_streaks",
    "store_stats",
]
