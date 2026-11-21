"""Read the finite daily-row state input once from the fixed market snapshot."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Collection
from datetime import date, datetime
from math import isfinite

from baibai_engine.machine_analysis.observations.prices.price_observation import (
    ExpectedPriceBar,
    NativePriceSession,
    PriceDayState,
    classify_price_day,
)
from baibai_engine.machine_analysis.observations.prices.sessions import latest_eligible_close_date
from baibai_engine.primitives.time import JST


def read_price_day_states(
    connection: sqlite3.Connection,
    *,
    start: date,
    end: date,
    tickers: Collection[str],
) -> dict[str, dict[date, PriceDayState]]:
    states, _ = read_price_day_observations(
        connection, start=start, end=end, tickers=tickers, expected_date=None
    )
    return states


def read_price_day_observations(
    connection: sqlite3.Connection,
    *,
    start: date,
    end: date,
    tickers: Collection[str],
    expected_date: date | None,
) -> tuple[dict[str, dict[date, PriceDayState]], dict[str, ExpectedPriceBar]]:
    """One range query for the whole subject population; no per-company query loop.

    Missing rows remain absent from the map. Action factors are independently read
    by the existing price/action readers, including on all-null price days.
    """
    subjects = tuple(sorted(set(tickers)))
    if not subjects:
        return {}, {}
    expected: dict[str, ExpectedPriceBar] = {}
    result: dict[str, dict[date, PriceDayState]] = defaultdict(dict)
    # Only placeholder count is interpolated; all tickers remain bound values.
    query = (
        "SELECT ticker,traded_at,open,high,low,close,volume,turnover_value "  # nosec B608
        "FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ? AND ticker IN ("
        + ",".join("?" for _ in subjects)
        + ") ORDER BY ticker,traded_at"
    )
    rows = connection.execute(
        query,
        (start.isoformat(), end.isoformat(), *subjects),
    )
    dates: dict[str, date] = {}
    for row in rows:
        day = dates.get(row[1])
        if day is None:
            day = date.fromisoformat(row[1])
            dates[row[1]] = day
        state = classify_price_day(row[2:])
        result[row[0]][day] = state
        if day == expected_date:
            close = row[5]
            expected[row[0]] = {
                "raw_close_yen": float(close)
                if isinstance(close, (float, int)) and isfinite(close)
                else None,
                "day_state": state,
            }
    return dict(result), expected


def read_native_price_session(
    connection: sqlite3.Connection,
    *,
    expected_date: date | None,
    known_at: datetime,
) -> NativePriceSession:
    row = None
    if expected_date is not None:
        day = expected_date.isoformat()
        row = connection.execute(
            "SELECT status,fetched_at_utc,error,record_count FROM source_coverage "
            "WHERE source='jquants_daily_bars' AND coverage_key=? AND coverage_start=? "
            "AND coverage_end=?",
            ("native:" + day, day, day),
        ).fetchone()
    if row is None:
        return NativePriceSession(
            expected_date=expected_date, status="not_recorded", recorded_at=None
        )
    status, clock, error, count = row
    try:
        at = datetime.fromisoformat(clock)
    except (TypeError, ValueError):
        at = None
    if (
        at is None
        or at.utcoffset() is None
        or at > known_at
        or (expected_date is not None and at.astimezone(JST).date() < expected_date)
    ):
        return NativePriceSession(
            expected_date=expected_date, status="unconfirmed", recorded_at=None
        )
    if status == "ok" and error is None and isinstance(count, int) and count >= 0:
        if expected_date is not None and latest_eligible_close_date(at) < expected_date:
            return NativePriceSession(
                expected_date=expected_date, status="unconfirmed", recorded_at=at
            )
        return NativePriceSession(
            expected_date=expected_date, status="ok" if count else "empty", recorded_at=at
        )
    return NativePriceSession(
        expected_date=expected_date,
        status="partial"
        if status == "partial"
        else "unavailable"
        if status == "unavailable"
        else "unconfirmed",
        recorded_at=at,
    )


def read_turnover_values(
    connection: sqlite3.Connection,
    *,
    start: date,
    end: date,
    tickers: Collection[str],
) -> dict[str, dict[date, float | None]]:
    """Read yen values independently of close availability or corporate-action basis.

    A partial price row can still contain a valid traded value. Keep zero, null
    and absent rows distinct; the activity owner decides numeric eligibility.
    """
    subjects = tuple(sorted(set(tickers)))
    if not subjects:
        return {}
    query = (
        "SELECT ticker,traded_at,turnover_value "  # nosec B608
        "FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ? AND ticker IN ("
        + ",".join("?" for _ in subjects)
        + ") ORDER BY ticker,traded_at"
    )
    rows = connection.execute(query, (start.isoformat(), end.isoformat(), *subjects))
    result: dict[str, dict[date, float | None]] = defaultdict(dict)
    dates: dict[str, date] = {}
    for ticker, recorded_day, value in rows:
        day = dates.get(recorded_day)
        if day is None:
            day = date.fromisoformat(recorded_day)
            dates[recorded_day] = day
        result[ticker][day] = float(value) if isinstance(value, (int, float)) else None
    return dict(result)
