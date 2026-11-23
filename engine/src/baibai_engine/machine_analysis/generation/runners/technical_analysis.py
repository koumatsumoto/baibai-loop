"""Read all retained prices once for one as-of Technical Analysis; never acquire or write."""

from __future__ import annotations

from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.machine_analysis.models.technical_analysis.generation import (
    generate_technical_analysis,
)
from baibai_engine.machine_analysis.models.technical_analysis.price_path import observe_price_path
from baibai_engine.machine_analysis.observations.prices.adjusted_prices import read_adjusted_bars
from baibai_engine.machine_analysis.observations.prices.calendar_context import (
    read_price_calendar,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.observations.prices.price_observation import (
    PriceCalendar,
    PriceDayState,
    price_calendar,
)
from baibai_engine.machine_analysis.observations.prices.price_observation_reader import (
    read_price_day_states,
)
from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.storage.sqlite.read import read_store_if_written
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only


def technical_analysis(path: Path, *, ticker: str, as_of: date) -> dict[str, object]:
    ticker = normalize_ticker(ticker)
    inputs = read_store_if_written(path, lambda: _price_inputs(path, ticker=ticker, as_of=as_of))
    bars, calendar, day_states = (
        ([], price_calendar({}, as_of=as_of), {}) if inputs is None else inputs
    )
    path_observation = observe_price_path(bars, calendar=calendar, day_states=day_states)
    return generate_technical_analysis(
        bars,
        ticker=ticker,
        as_of=as_of,
        trading_dates=calendar.trading_dates,
        price_path=path_observation.payload,
    )


def _price_inputs(
    path: Path, *, ticker: str, as_of: date
) -> tuple[list[PriceBar], PriceCalendar, dict[date, PriceDayState]]:
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        bars = read_adjusted_bars(connection, tickers=(ticker,), start=date.min, end=as_of)
        calendar = read_price_calendar(connection, as_of=as_of)
        windows = price_calendar(calendar, as_of=as_of)
        start = min(
            (window[0] for window in windows.windows.values() if window is not None),
            default=windows.expected_price_date or as_of,
        )
        states = read_price_day_states(connection, start=start, end=as_of, tickers=(ticker,))
    return bars.get(ticker, []), windows, states.get(ticker, {})
