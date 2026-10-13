"""Query-only market price views for read-only application consumers."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from baibai_engine.market_context.prices import close_change_since, worst_close_drawdown
from baibai_engine.storage.observations.read import connect_read_only

from .sqlite import read_rows as _read_rows


def read_rows(path: Path, sql: str, parameters: Sequence[object] = ()) -> list[sqlite3.Row]:
    return _read_rows(path, sql, parameters, connector=connect_read_only)


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


def latest_market_bar_date(path: Path) -> date | None:
    """Return the newest day the bar store has a price for, or None when it has none.

    A window a caller asks for can run past the data. Reporting the requested end
    would present a conclusion drawn from a shorter observation than it claims.
    """

    rows = read_rows(path, "SELECT MAX(traded_at) FROM jquants_daily_bars WHERE close IS NOT NULL")
    value = rows[0][0] if rows else None
    return None if value is None else date.fromisoformat(str(value))


def previous_business_day(path: Path, day: date, *, max_lookback: int = 10) -> date | None:
    """Return the latest trading day strictly before ``day``, or None when unknown.

    A comparison against "yesterday" has to skip weekends and holidays, and the
    calendar is the only record of which those are. This answers a view question, so
    it degrades: a store the writer has not produced yields None and the caller
    reports the comparison as unmeasured. ``market_calendar_business_day`` raises
    instead because it gates whether the daily batch writes at all.
    """

    rows = read_rows(
        path,
        """
            SELECT day FROM jquants_market_calendar
            WHERE day < ? AND day >= ? AND is_business_day = 1
            ORDER BY day DESC LIMIT 1
        """,
        (day.isoformat(), (day - timedelta(days=max_lookback)).isoformat()),
    )
    return date.fromisoformat(str(rows[0][0])) if rows else None


def latest_disclosure_dates_after(
    path: Path, tickers: Sequence[str], *, after: date
) -> dict[str, date]:
    """Return each ticker's newest financial disclosure strictly after ``after``.

    A candidate that entered the Review Set right after reporting is a different thing
    from one that entered on a price move alone, and the screen's own output does
    not carry the disclosure date. A ticker with no disclosure in the window yields
    no entry.
    """

    if not tickers:
        return {}
    unique = list(dict.fromkeys(tickers))
    placeholders = ",".join("?" for _ in unique)
    # The f-string only expands "?" placeholders; every value is parameter-bound.
    rows = read_rows(
        path,
        f"""
            SELECT ticker, MAX(disclosed_at)
            FROM jquants_fin_summaries
            WHERE ticker IN ({placeholders}) AND disclosed_at > ?
            GROUP BY ticker
            """,  # nosec B608
        [*unique, after.isoformat()],
    )
    return {str(row[0]): date.fromisoformat(str(row[1])) for row in rows if row[1] is not None}


def next_earnings_dates(path: Path, tickers: Sequence[str], *, asof: date) -> dict[str, date]:
    """Return each ticker's earliest scheduled JPX earnings announcement on/after ``asof``.

    Reads the licensed market store's JPX earnings calendar read-only (physical table
    ``jpx_earnings_calendar``, logical source ``jpx_earnings_calendar``). A missing
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
            FROM jpx_earnings_calendar
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


__all__ = [
    "close_change_since",
    "latest_disclosure_dates_after",
    "latest_market_bar_date",
    "latest_unadjusted_closes",
    "market_calendar_business_day",
    "next_earnings_dates",
    "previous_business_day",
    "worst_close_drawdown",
]


def stored_market_rows(
    path: Path,
    *,
    kind: str,
    filters: dict[str, object],
    after: list[str | int | float] | None = None,
    limit: int = 1,
) -> list[dict[str, object]]:
    from baibai_engine.storage.observations.stored import local_rows

    from .sqlite import connect_read_only
    from .stored import required_read

    with required_read(path, connect_read_only) as connection:
        return local_rows(connection, kind=kind, filters=filters, after=after, limit=limit)
