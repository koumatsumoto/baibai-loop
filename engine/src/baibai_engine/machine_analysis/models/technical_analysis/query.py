"""Read all retained prices once for one as-of Technical Analysis; never acquire or write."""

from __future__ import annotations

from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.data_collection.identifiers import normalize_ticker
from baibai_engine.storage.sqlite.read import read_store_if_written
from baibai_engine.market.adjusted_prices import read_price_bars
from baibai_engine.market.calendar_context import price_calendar_basis, read_price_calendar
from baibai_engine.market.price_observation import PriceDayState, price_calendar
from baibai_engine.market.price_observation_reader import read_price_day_states
from baibai_engine.storage.observations.read import connect_read_only

from baibai_engine.machine_analysis.models.technical_analysis.generation import generate_technical_analysis
from baibai_engine.machine_analysis.models.technical_analysis.price_path import observe_price_path


def technical_analysis(path: Path, *, ticker: str, as_of: date) -> dict[str, object]:
    ticker = normalize_ticker(ticker)
    bars = read_price_bars(path, tickers=(ticker,), start=date.min, end=as_of).get(ticker, [])
    inputs = read_store_if_written(path, lambda: _price_inputs(path, ticker=ticker, as_of=as_of))
    calendar, day_states = inputs if inputs is not None else ({}, {})
    path_observation = observe_price_path(
        bars, calendar=price_calendar(calendar, as_of=as_of), day_states=day_states
    )
    return generate_technical_analysis(
        bars,
        ticker=ticker,
        as_of=as_of,
        trading_dates=price_calendar_basis(calendar, as_of=as_of)[1],
        price_path=path_observation.payload,
    )


def _price_inputs(
    path: Path, *, ticker: str, as_of: date
) -> tuple[dict[date, bool], dict[date, PriceDayState]]:
    with closing(connect_read_only(path)) as connection:
        calendar = read_price_calendar(connection, as_of=as_of)
        windows = price_calendar(calendar, as_of=as_of)
        start = min(
            (window[0] for window in windows.windows.values() if window is not None),
            default=windows.expected_price_date or as_of,
        )
        states = read_price_day_states(connection, start=start, end=as_of, tickers=(ticker,))
    return calendar, states.get(ticker, {})
