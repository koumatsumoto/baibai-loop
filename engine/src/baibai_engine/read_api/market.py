"""Query-only market price views for read-only application consumers."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from baibai_engine.market.bars import (
    JQuantsAdjustmentFactorEvent,
    JQuantsDailyBar,
    asof_basis_closes,
)
from baibai_engine.market.sqlite.read import connect_read_only

from .sqlite import read_rows as _read_rows

# How far before the requested start a bar may sit: the market is closed for up to
# a week around the New Year, so a shorter window would drop the comparison端.
_CHANGE_START_LOOKBACK_DAYS = 15


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
    events_by_ticker = _adjustment_events_by_ticker(
        path,
        unique,
        start=since - timedelta(days=_CHANGE_START_LOOKBACK_DAYS),
    )
    changes: dict[str, float] = {}
    for ticker, bars in by_ticker.items():
        start = _index_on_or_before(bars, since)
        if start is None or start == len(bars) - 1:
            continue
        closes = asof_basis_closes(
            bars,
            events_by_ticker.get(ticker, ()),
            asof_date=bars[-1].traded_at,
        )
        if closes[start] == 0:
            continue
        changes[ticker] = round((closes[-1] / closes[start] - 1) * 100, 1)
    return changes


def _index_on_or_before(bars: Sequence[JQuantsDailyBar], day: date) -> int | None:
    for index in range(len(bars) - 1, -1, -1):
        if bars[index].traded_at <= day:
            return index
    return None


def worst_close_drawdown(
    path: Path, tickers: Sequence[str], *, start: date, end: date
) -> dict[str, float]:
    """Return each ticker's deepest close-to-close fall from ``start`` within the window.

    A permanent-loss judgment is about how far a name can fall while it is held, which
    the return at a single later date does not show: a name that halved and recovered
    reads as flat. Both ends are put on the window's latest share basis so a split does
    not register as a fall.

    The value is a ratio (``-0.3`` for a 30% trough) and is never positive: a name
    that stayed above its entry did not fall. A ticker with no close at or before
    ``start`` yields no entry.
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
              AND traded_at <= ?
            ORDER BY ticker, traded_at
            """,  # nosec B608
        [
            *unique,
            (start - timedelta(days=_CHANGE_START_LOOKBACK_DAYS)).isoformat(),
            end.isoformat(),
        ],
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
    events_by_ticker = _adjustment_events_by_ticker(
        path,
        unique,
        start=start - timedelta(days=_CHANGE_START_LOOKBACK_DAYS),
        end=end,
    )
    worst: dict[str, float] = {}
    for ticker, bars in by_ticker.items():
        entry = _index_on_or_before(bars, start)
        if entry is None or entry == len(bars) - 1:
            continue
        closes = asof_basis_closes(
            bars,
            events_by_ticker.get(ticker, ()),
            asof_date=end,
        )
        if closes[entry] == 0:
            continue
        trough = min(closes[entry + 1 :])
        # A name that never traded below its entry has no drawdown. Reporting the
        # distance to its lowest point would call a rise a fall.
        worst[ticker] = min(0.0, trough / closes[entry] - 1)
    return worst


def _adjustment_events_by_ticker(
    path: Path,
    tickers: Sequence[str],
    *,
    start: date,
    end: date | None = None,
) -> dict[str, list[JQuantsAdjustmentFactorEvent]]:
    """Read action events independently of close availability."""

    if not tickers:
        return {}
    placeholders = ",".join("?" for _ in tickers)
    end_clause = " AND traded_at <= ?" if end is not None else ""
    parameters: list[object] = [*tickers, start.isoformat()]
    if end is not None:
        parameters.append(end.isoformat())
    rows = read_rows(
        path,
        f"""
            SELECT ticker, traded_at, adjustment_factor
            FROM jquants_daily_bars
            WHERE ticker IN ({placeholders})
              AND traded_at >= ?
              {end_clause}
              AND adjustment_factor IS NOT NULL
              AND adjustment_factor NOT IN (0.0, 1.0)
            ORDER BY ticker, traded_at
            """,  # nosec B608
        parameters,
    )
    grouped: dict[str, list[JQuantsAdjustmentFactorEvent]] = {}
    for ticker, traded_at, factor in rows:
        grouped.setdefault(str(ticker), []).append(
            JQuantsAdjustmentFactorEvent(
                ticker=str(ticker),
                traded_at=date.fromisoformat(str(traded_at)),
                adjustment_factor=float(factor),
            )
        )
    return grouped


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
    from baibai_engine.market.sqlite.stored import local_rows

    from .sqlite import connect_read_only
    from .stored import required_read

    with required_read(path, connect_read_only) as connection:
        return local_rows(connection, kind=kind, filters=filters, after=after, limit=limit)
