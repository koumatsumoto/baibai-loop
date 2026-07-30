"""Query-only market price views for read-only application consumers."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from baibai_engine.market.bars import JQuantsDailyBar, asof_basis_closes

from .sqlite import connect_read_only, read_rows

# How far before the requested start a bar may sit: the market is closed for up to
# a week around the New Year, so a shorter window would drop the comparison端.
_CHANGE_START_LOOKBACK_DAYS = 15


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


def close_change_since(path: Path, tickers: Sequence[str], *, since: date) -> dict[str, float]:
    """Return each ticker's percent close change from ``since`` to its latest close.

    Both ends are put on the latest share basis before dividing. The stored close is
    unadjusted and ``adjustment_close`` mixes vintages across an incremental cache, so
    a split between the two dates would otherwise read as a price move of the split
    ratio — a 1:2 split as a 50% fall. The correction uses ``adjustment_factor``,
    which is the split event itself and does not change once published.

    A ticker with no close at or before ``since`` yields no entry: there is nothing to
    compare against, which is different from having not moved.
    """

    if not tickers:
        return {}
    unique = list(dict.fromkeys(tickers))
    placeholders = ",".join("?" for _ in unique)
    # The f-string only expands "?" placeholders; every value is parameter-bound.
    rows = read_rows(
        path,
        f"""
            SELECT ticker, traded_at, close, adjustment_factor
            FROM jquants_daily_bars
            WHERE ticker IN ({placeholders})
              AND close IS NOT NULL
              AND traded_at >= ?
            ORDER BY ticker, traded_at
            """,  # nosec B608
        [*unique, (since - timedelta(days=_CHANGE_START_LOOKBACK_DAYS)).isoformat()],
    )
    by_ticker: dict[str, list[JQuantsDailyBar]] = {}
    for ticker, traded_at, close, factor in rows:
        by_ticker.setdefault(str(ticker), []).append(
            JQuantsDailyBar(
                ticker=str(ticker),
                traded_at=date.fromisoformat(str(traded_at)),
                close=float(close),
                turnover_value=None,
                adjustment_factor=None if factor is None else float(factor),
            )
        )
    changes: dict[str, float] = {}
    for ticker, bars in by_ticker.items():
        start = _index_on_or_before(bars, since)
        if start is None or start == len(bars) - 1:
            continue
        closes = asof_basis_closes(bars)
        if closes[start] == 0:
            continue
        changes[ticker] = round((closes[-1] / closes[start] - 1) * 100, 1)
    return changes


def _index_on_or_before(bars: Sequence[JQuantsDailyBar], day: date) -> int | None:
    for index in range(len(bars) - 1, -1, -1):
        if bars[index].traded_at <= day:
            return index
    return None


def latest_disclosure_dates_after(
    path: Path, tickers: Sequence[str], *, after: date
) -> dict[str, date]:
    """Return each ticker's newest financial disclosure strictly after ``after``.

    A candidate that entered the pool right after reporting is a different thing
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


__all__ = [
    "close_change_since",
    "latest_disclosure_dates_after",
    "latest_unadjusted_closes",
    "market_calendar_business_day",
    "next_earnings_dates",
    "previous_business_day",
]
