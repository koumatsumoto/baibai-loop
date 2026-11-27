"""Read raw prices and action events over explicitly covered ranges."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Set
from datetime import date
from itertools import batched, chain
from pathlib import Path
from typing import Any

from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.prices.price_models import (
    JQuantsAdjustmentFactorEvent,
    JQuantsDailyBar,
)
from baibai_engine.storage.sqlite.source_cache.convert import optional_float
from baibai_engine.storage.sqlite.source_cache.coverage import range_covered, uncovered_days
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


def read_ticker_bars(
    conn: sqlite3.Connection, marks_by_session: Mapping[date, Set[str]]
) -> list[JQuantsDailyBar] | None:
    """Read exact closing marks; an empty set still requires that session's receipt."""
    if not marks_by_session:
        return []
    if uncovered_days(conn, "jquants_daily_bars", marks_by_session):
        return None
    pairs = sorted(
        (ticker, day.isoformat()) for day, tickers in marks_by_session.items() for ticker in tickers
    )
    bars: list[JQuantsDailyBar] = []
    # Bound the two parameters per key below SQLite's traditional 999-variable limit.
    for batch in batched(pairs, 400, strict=False):
        placeholders = ", ".join("(?, ?)" for _ in batch)
        rows = conn.execute(
            f"WITH marks(ticker, traded_at) AS (VALUES {placeholders}) "  # nosec B608
            "SELECT b.ticker, b.traded_at, b.close, b.turnover_value, b.volume, "
            "b.adjustment_factor, b.ex_right_type FROM marks "
            "JOIN jquants_daily_bars b USING (ticker, traded_at) ORDER BY b.ticker, b.traded_at",
            tuple(chain.from_iterable(batch)),
        )
        bars.extend(_daily_bars_from_rows(rows))
    return bars


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
        return read_latest_daily_bar_date(conn, start, end)
    finally:
        conn.close()


def read_latest_daily_bar_date(
    connection: sqlite3.Connection, start: date, end: date
) -> date | None:
    """Resolve a stored trading day on the caller's market snapshot."""
    row = connection.execute(
        "SELECT MAX(traded_at) FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
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
