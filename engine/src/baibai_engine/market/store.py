"""Market価格とcalendarの読取。財務・銘柄・信用等はstorage.observations.readerが所有する。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from baibai_engine.market.bars import (
    JQuantsAdjustmentFactorEvent,
    JQuantsDailyBar,
    JQuantsMarketCalendarDay,
)
from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.storage.observations.convert import optional_float
from baibai_engine.storage.observations.coverage import daily_bars_covered_by_data, range_covered
from baibai_engine.storage.observations.schema import connect_current


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
            "SELECT ticker, traded_at, close, turnover_value, volume, "
            "adjustment_factor, ex_right_type "
            "FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ? "
            "ORDER BY ticker, traded_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()

    return _daily_bars_from_rows(rows)


def daily_bars_covered(sqlite_path: Path, start: date, end: date) -> bool:
    """Answer whether the cache can serve `[start, end]` without building the rows.

    `read_daily_bars` answers the same question, but only as a side effect of
    materialising every row in the window. A 1,200-day window is ~3.4M rows, and
    the callers that only need the yes/no (the fetch planner, the chunk skip test)
    would pay for a model per row to read one boolean.

    Coverage here is about which dates are present, so a row that cannot be turned
    into a bar is not detected: `read_daily_bars` raises on one, this does not. The
    screen builds its inputs through that reader, which is where such a row surfaces.
    """
    if not sqlite_path.exists():
        return False
    conn = connect_current(sqlite_path)
    if conn is None:
        return False
    try:
        return daily_bars_covered_by_data(conn, start, end)
    finally:
        conn.close()


def count_daily_bars(sqlite_path: Path, start: date, end: date) -> int:
    """Count non-null closes; return 0 for an unavailable or incompatible store.

    This does not validate individual values or prove acquisition coverage.
    Query errors propagate; model validation belongs to the row reader.
    """
    if not sqlite_path.exists():
        return 0
    conn = connect_current(sqlite_path)
    if conn is None:
        return 0
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM jquants_daily_bars "
            "WHERE traded_at BETWEEN ? AND ? AND close IS NOT NULL",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0] or 0) if row is not None else 0


def read_adjustment_factor_bars(
    sqlite_path: Path, start: date, end: date
) -> list[JQuantsAdjustmentFactorEvent] | None:
    """Read split events over a covered range, including rows without a close."""
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not daily_bars_covered_by_data(conn, start, end):
            return None
        rows = conn.execute(
            "SELECT ticker, traded_at, adjustment_factor, ex_right_type "
            "FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ? "
            "AND adjustment_factor != 0.0 "
            "AND (adjustment_factor != 1.0 OR ex_right_type IS NOT NULL) "
            "ORDER BY ticker, traded_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()
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
        if not daily_bars_covered_by_data(conn, start, end):
            return None
        placeholders = ", ".join("?" for _ in tickers)
        rows = conn.execute(
            "SELECT ticker, traded_at, close, turnover_value, volume, "
            "adjustment_factor, ex_right_type "
            "FROM jquants_daily_bars "
            # Local placeholder count; ticker values remain bound.  # nosec B608
            f"WHERE ticker IN ({placeholders}) AND traded_at BETWEEN ? AND ? "
            "ORDER BY ticker, traded_at",
            (*tickers, start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()
    return _daily_bars_from_rows(rows)


def latest_stored_daily_bar_date(sqlite_path: Path) -> date | None:
    """Return the most recent ``traded_at`` anywhere in the store, or None.

    The fetch planner uses this to avoid implicitly refreshing accepted historical
    windows. It is a reuse policy, not proof that older dates cannot be revised.
    """
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT MAX(traded_at) FROM jquants_daily_bars").fetchone()
    finally:
        conn.close()
    if row is None or row[0] is None:
        return None
    try:
        return date.fromisoformat(str(row[0]))
    except ValueError:
        return None


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
    finally:
        conn.close()
    return [
        JQuantsMarketCalendarDay(day=date.fromisoformat(day), is_business_day=bool(flag))
        for day, flag in rows
    ]


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
