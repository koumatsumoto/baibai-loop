"""Read finite daily-row state and turnover inputs from a fixed market snapshot."""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from datetime import date
from itertools import groupby
from operator import itemgetter

from baibai_engine.machine_analysis.observations.prices.price_observation import (
    PriceDayState,
    classify_price_day,
)

type PriceDayStates = dict[str, dict[date, PriceDayState]]
type TurnoverValues = dict[str, dict[date, float | None]]


def read_price_day_states(
    connection: sqlite3.Connection,
    *,
    start: date,
    end: date,
    tickers: Collection[str],
) -> PriceDayStates:
    """Read strict day states only, including for standalone technical analysis."""
    return read_price_day_inputs(connection, start=start, end=end, tickers=tickers)[0]


def read_price_day_inputs(
    connection: sqlite3.Connection,
    *,
    start: date,
    end: date,
    tickers: Collection[str],
    turnover_start: date | None = None,
    turnover_tickers: Collection[str] = (),
) -> tuple[PriceDayStates, TurnoverValues]:
    """Read one native range; raw yen values keep their own narrower scope.

    Missing rows remain absent. A partial price row can carry a traded value;
    zero, null and absence are distinct before the activity owner validates it.
    Action factors and adjusted histories remain separate source reads.
    """
    subjects = tuple(sorted(set(tickers)))
    turnover_subjects = set(turnover_tickers)
    if turnover_subjects and (
        turnover_start is None or turnover_start < start or not turnover_subjects.issubset(subjects)
    ):
        raise ValueError("turnover scope must be within the price day input scope")
    if not subjects:
        return {}, {}
    floor = turnover_start.isoformat() if turnover_start is not None else ""
    # Only placeholder count is interpolated; all tickers remain bound values.
    query = (
        "SELECT ticker,traded_at,open,high,low,close,volume,turnover_value "  # nosec B608
        "FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ? AND ticker IN ("
        + ",".join("?" for _ in subjects)
        + ") ORDER BY ticker,traded_at"
    )
    rows = connection.execute(query, (start.isoformat(), end.isoformat(), *subjects))
    dates: dict[str, date] = {}
    states: PriceDayStates = {}
    turnover: TurnoverValues = {}
    for ticker, ticker_rows in groupby(rows, key=itemgetter(0)):
        ticker_states: dict[date, PriceDayState] = {}
        states[ticker] = ticker_states
        ticker_turnover: dict[date, float | None] | None = (
            {} if ticker in turnover_subjects else None
        )
        for row in ticker_rows:
            recorded_at = row[1]
            day = dates.get(recorded_at)
            if day is None:
                day = dates[recorded_at] = date.fromisoformat(recorded_at)
            ticker_states[day] = classify_price_day(row[2:])
            if ticker_turnover is not None and recorded_at >= floor:
                value = row[7]
                ticker_turnover[day] = float(value) if isinstance(value, (int, float)) else None
        if ticker_turnover:
            turnover[ticker] = ticker_turnover
    return states, turnover
