"""Read raw prices and action events over explicitly covered ranges."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.prices.price_models import (
    JQuantsAdjustmentFactorEvent,
    JQuantsDailyBar,
)
from baibai_engine.storage.sqlite.source_cache.convert import optional_float
from baibai_engine.storage.sqlite.source_cache.coverage import range_covered
from baibai_engine.storage.sqlite.source_cache.schema import connect_current


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
        conn.execute("BEGIN")
        if not range_covered(conn, "jquants_daily_bars", start, end):
            return None
        rows = conn.execute(
            "SELECT ticker, traded_at, close, turnover_value, volume, "
            "adjustment_factor, ex_right_type "
            "FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ? "
            "ORDER BY ticker, traded_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()

    return _daily_bars_from_rows(rows)


def read_adjustment_factor_bars(
    sqlite_path: Path, start: date, end: date
) -> list[JQuantsAdjustmentFactorEvent] | None:
    """Read price-adjustment events over an accepted range, including rows without a close."""
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        conn.execute("BEGIN")
        if not range_covered(conn, "jquants_daily_bars", start, end):
            return None
        rows = conn.execute(
            "SELECT ticker, traded_at, adjustment_factor, ex_right_type "
            "FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ? "
            "AND (adjustment_factor != 1.0 OR ex_right_type IS NOT NULL) "
            "ORDER BY ticker, traded_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()
    return _adjustment_events_from_rows(rows)


def _adjustment_events_from_rows(
    rows: Iterable[tuple[Any, ...]],
) -> list[JQuantsAdjustmentFactorEvent]:
    """Decode the same action rows for full-range and single-ticker readers."""
    events: list[JQuantsAdjustmentFactorEvent] = []
    for ticker, traded_at, adjustment_factor, ex_right_type in rows:
        if traded_at is None:
            continue
        try:
            events.append(
                JQuantsAdjustmentFactorEvent(
                    ticker=str(ticker),
                    traded_at=date.fromisoformat(traded_at),
                    adjustment_factor=float(adjustment_factor),
                    ex_right_type=ex_right_type,
                )
            )
        except (TypeError, ValueError) as exc:
            raise JQuantsProviderError(
                f"corrupt adjustment factor row for {ticker} on {traded_at}: {exc}"
            ) from exc
    return events


def read_daily_bars_for_tickers(
    sqlite_path: Path,
    tickers: tuple[str, ...],
    start: date,
    end: date,
) -> list[JQuantsDailyBar] | None:
    """Read only the requested instruments over a covered range.

    Portfolio outcome can span five years, but it must never load the full
    exchange universe merely to value the repository's own holdings. Only
    observed rows with a close are returned; absent ticker-days are not filled.
    The outcome engine detects those gaps rather than inventing a close.
    """

    if not tickers:
        return []
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        conn.execute("BEGIN")
        return _read_ticker_bars(conn, tickers, start, end)
    finally:
        conn.close()


def _read_ticker_bars(
    conn: sqlite3.Connection, tickers: tuple[str, ...], start: date, end: date
) -> list[JQuantsDailyBar] | None:
    if not tickers:
        return []
    if not range_covered(conn, "jquants_daily_bars", start, end):
        return None
    placeholders = ", ".join("?" for _ in tickers)
    rows = conn.execute(
        "SELECT ticker, traded_at, close, turnover_value, volume, "
        "adjustment_factor, ex_right_type FROM jquants_daily_bars "
        # Local placeholder count; ticker values remain bound.  # nosec B608
        f"WHERE ticker IN ({placeholders}) AND traded_at BETWEEN ? AND ? "
        "ORDER BY ticker, traded_at",
        (*tickers, start.isoformat(), end.isoformat()),
    ).fetchall()
    return _daily_bars_from_rows(rows)


def latest_daily_bar_date(sqlite_path: Path, start: date, end: date) -> date | None:
    """Return the most recent ``traded_at`` stored within ``[start, end]``, or None.

    Lets a fetch-capable bootstrap explicitly recheck the recent tail when the
    recorded acquisition is complete but the end date has no stored bar.
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
    return date.fromisoformat(str(row[0]))


def _daily_bars_from_rows(rows: Iterable[tuple[Any, ...]]) -> list[JQuantsDailyBar]:
    """Decode the same selected columns for full-universe and scoped reads."""
    bars: list[JQuantsDailyBar] = []
    for (
        ticker,
        traded_at,
        close,
        turnover_value,
        volume,
        adjustment_factor,
        ex_right_type,
    ) in rows:
        if close is None or traded_at is None:
            continue
        try:
            bars.append(
                JQuantsDailyBar(
                    ticker=str(ticker),
                    traded_at=date.fromisoformat(traded_at),
                    close=float(close),
                    turnover_value=optional_float(turnover_value),
                    volume=optional_float(volume),
                    adjustment_factor=optional_float(adjustment_factor),
                    ex_right_type=ex_right_type,
                )
            )
        except (TypeError, ValueError) as exc:
            raise JQuantsProviderError(
                f"corrupt SQLite row in jquants_daily_bars for {ticker} on {traded_at}: {exc}"
            ) from exc
    return bars
