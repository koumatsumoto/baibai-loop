"""Read helpers that pull price/calendar inputs from the canonical SQLite store.

The functions are deliberately permissive: a missing SQLite file or a source
that has not been fetched yet returns `None` so bootstrap/fetch commands can
populate the missing coverage. Screening's fundamentals reads live alongside in
`screening.sqlite_reader`; this module owns daily bars and the market calendar.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from baibai_loop.market.bars import JQuantsDailyBar, JQuantsMarketCalendarDay
from baibai_loop.market.jquants import JQuantsProviderError
from baibai_loop.market.sqlite import (
    connect_current,
    daily_bars_covered_by_data,
    optional_float,
    range_covered,
)


def read_daily_bars(sqlite_path: Path, start: date, end: date) -> list[JQuantsDailyBar] | None:
    """Return daily bars for `[start, end]` from SQLite, or `None` if the
    cache cannot serve the full range.
    """
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not daily_bars_covered_by_data(conn, start, end):
            return None
        rows = conn.execute(
            "SELECT ticker, traded_at, close, turnover_value, adjustment_close, adjustment_factor "
            "FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ? "
            "ORDER BY ticker, traded_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()

    bars: list[JQuantsDailyBar] = []
    for ticker, traded_at, close, turnover_value, adjustment_close, adjustment_factor in rows:
        if close is None or traded_at is None:
            continue
        try:
            bars.append(
                JQuantsDailyBar(
                    ticker=str(ticker),
                    traded_at=date.fromisoformat(traded_at),
                    close=float(close),
                    turnover_value=optional_float(turnover_value),
                    adjustment_close=optional_float(adjustment_close),
                    adjustment_factor=optional_float(adjustment_factor),
                )
            )
        except (TypeError, ValueError) as exc:
            raise JQuantsProviderError(
                f"corrupt SQLite row in jquants_daily_bars for {ticker} on {traded_at}: {exc}"
            ) from exc
    return bars


def read_daily_bars_for_tickers(
    sqlite_path: Path,
    tickers: tuple[str, ...],
    start: date,
    end: date,
) -> list[JQuantsDailyBar] | None:
    """Read only the requested instruments over a covered range.

    Portfolio outcome can span five years, but it must never load the full
    exchange universe merely to value the repository's own holdings. Missing
    ticker-day rows are deliberately returned to the caller; the outcome engine
    classifies them as unresolved instead of inventing a close.
    """

    if not tickers:
        return []
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not daily_bars_covered_by_data(conn, start, end):
            return None
        placeholders = ", ".join("?" for _ in tickers)
        rows = conn.execute(
            "SELECT ticker, traded_at, close, turnover_value, adjustment_close, adjustment_factor "
            "FROM jquants_daily_bars "
            # Local placeholder count; ticker values remain bound.  # nosec B608
            f"WHERE ticker IN ({placeholders}) AND traded_at BETWEEN ? AND ? "
            "ORDER BY ticker, traded_at",
            (*tickers, start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()
    bars: list[JQuantsDailyBar] = []
    for ticker, traded_at, close, turnover_value, adjustment_close, adjustment_factor in rows:
        if close is None or traded_at is None:
            continue
        try:
            bars.append(
                JQuantsDailyBar(
                    ticker=str(ticker),
                    traded_at=date.fromisoformat(traded_at),
                    close=float(close),
                    turnover_value=optional_float(turnover_value),
                    adjustment_close=optional_float(adjustment_close),
                    adjustment_factor=optional_float(adjustment_factor),
                )
            )
        except (TypeError, ValueError) as exc:
            raise JQuantsProviderError(
                f"corrupt SQLite row in jquants_daily_bars for {ticker} on {traded_at}: {exc}"
            ) from exc
    return bars


def latest_daily_bar_date(sqlite_path: Path, start: date, end: date) -> date | None:
    """Return the most recent ``traded_at`` stored within ``[start, end]``, or None.

    Lets a fetch-capable bootstrap decide whether it still needs the recent tail:
    ``read_daily_bars`` tolerates a holiday-sized edge gap, so it can report a
    window covered while the asof's own bar is not yet stored.
    """
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT MAX(traded_at) FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
    finally:
        conn.close()
    if row is None or row[0] is None:
        return None
    try:
        return date.fromisoformat(str(row[0]))
    except ValueError:
        return None


def read_market_calendar(
    sqlite_path: Path, start: date, end: date
) -> list[JQuantsMarketCalendarDay] | None:
    """Return market calendar days for `[start, end]`, or `None` if the
    cache cannot serve the range.
    """
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not range_covered(conn, "jquants_market_calendar", start, end):
            return None
        rows = conn.execute(
            "SELECT day, is_business_day FROM jquants_market_calendar "
            "WHERE day BETWEEN ? AND ? ORDER BY day",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return [
        JQuantsMarketCalendarDay(day=date.fromisoformat(day), is_business_day=bool(flag))
        for day, flag in rows
    ]
