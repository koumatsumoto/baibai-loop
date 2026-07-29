"""Query-only market price views for read-only application consumers."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import closing
from datetime import date
from pathlib import Path

from .sqlite import connect_read_only, read_rows


def market_calendar_business_day(path: Path, day: date) -> bool | None:
    """Return whether ``day`` is a trading day, or None when the calendar has no such row.

    This answer gates whether the daily batch writes at all, so unlike the view
    readers here it does not degrade: every way of failing to read the calendar
    raises and reaches the caller as its own diagnosis. An absent file, an
    unreadable store and an uncovered date each send the operator somewhere
    different, and collapsing them into "not a trading day" would skip a run that
    should have been reported as broken.
    """

    with closing(connect_read_only(path)) as connection:
        row = connection.execute(
            "SELECT is_business_day FROM jquants_market_calendar WHERE day = ?",
            (day.isoformat(),),
        ).fetchone()
    return None if row is None else bool(row[0])


def latest_unadjusted_closes(path: Path, tickers: Sequence[str]) -> dict[str, tuple[float, date]]:
    """Return each ticker's most recent non-null unadjusted close and its trade date.

    Reads the licensed daily-bars store read-only. A missing file or an unknown
    ticker yields no entry so callers fall back to the canonical ledger observation.
    Rows whose close is NULL are ignored.
    """

    if not tickers:
        return {}
    unique = list(dict.fromkeys(tickers))
    placeholders = ",".join("?" for _ in unique)
    # The f-string only expands "?" placeholders; every value is parameter-bound.
    rows = read_rows(
        path,
        f"""
            SELECT ticker, traded_at, close FROM (
                SELECT ticker, traded_at, close,
                       row_number() OVER (
                           PARTITION BY ticker ORDER BY traded_at DESC
                       ) AS rank
                FROM jquants_daily_bars
                WHERE ticker IN ({placeholders}) AND close IS NOT NULL
            )
            WHERE rank = 1
            """,  # nosec B608
        unique,
    )
    return {str(row[0]): (float(row[2]), date.fromisoformat(str(row[1]))) for row in rows}


def next_earnings_dates(path: Path, tickers: Sequence[str], *, asof: date) -> dict[str, date]:
    """Return each ticker's earliest scheduled JPX earnings announcement on/after ``asof``.

    Reads the licensed market store's JPX earnings calendar read-only (physical table
    ``jquants_earnings_calendar``, logical source ``jpx_earnings_calendar``). A missing
    file or a ticker with no announcement on/after ``asof`` yields no entry, so callers
    treat the earnings date as unknown rather than surfacing a stale schedule.
    """

    if not tickers:
        return {}
    unique = list(dict.fromkeys(tickers))
    placeholders = ",".join("?" for _ in unique)
    # The f-string only expands "?" placeholders; every value is parameter-bound.
    rows = read_rows(
        path,
        f"""
            SELECT ticker, MIN(announcement_date) AS next_date
            FROM jquants_earnings_calendar
            WHERE ticker IN ({placeholders}) AND announcement_date >= ?
            GROUP BY ticker
            """,  # nosec B608
        [*unique, asof.isoformat()],
    )
    result: dict[str, date] = {}
    for row in rows:
        if row[1] is None:
            continue
        result[str(row[0])] = date.fromisoformat(str(row[1]))
    return result


__all__ = ["latest_unadjusted_closes", "market_calendar_business_day", "next_earnings_dates"]
