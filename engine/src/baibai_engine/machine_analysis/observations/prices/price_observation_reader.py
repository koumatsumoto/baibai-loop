"""Read the finite daily-row state input once from the fixed market snapshot."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Collection
from datetime import date

from baibai_engine.machine_analysis.observations.prices.price_observation import (
    PriceDayState,
    classify_price_day,
)


def read_price_day_states(
    connection: sqlite3.Connection,
    *,
    start: date,
    end: date,
    tickers: Collection[str],
) -> dict[str, dict[date, PriceDayState]]:
    """One range query for the whole subject population; no per-company query loop.

    Missing rows remain absent from the map. Action factors are independently read
    by the existing price/action readers, including on all-null price days.
    """
    subjects = tuple(sorted(set(tickers)))
    if not subjects:
        return {}
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
        result[row[0]][day] = classify_price_day(row[2:])
    return dict(result)
