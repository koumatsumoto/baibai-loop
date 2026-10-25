"""Read all retained prices once for one as-of Technical Analysis; never acquire or write."""

from __future__ import annotations

from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.data_collection.identifiers import normalize_ticker
from baibai_engine.foundation.sqlite_read import read_store_if_written
from baibai_engine.market.adjusted_prices import read_price_bars
from baibai_engine.market.calendar_context import read_price_calendar_basis
from baibai_engine.storage.observations.read import connect_read_only

from .generation import generate_technical_analysis


def technical_analysis(path: Path, *, ticker: str, as_of: date) -> dict[str, object]:
    ticker = normalize_ticker(ticker)
    bars = read_price_bars(path, tickers=(ticker,), start=date.min, end=as_of).get(ticker, [])
    trading_dates = read_store_if_written(path, lambda: _trading_dates(path, as_of=as_of)) or ()
    return generate_technical_analysis(
        bars, ticker=ticker, as_of=as_of, trading_dates=trading_dates
    )


def _trading_dates(path: Path, *, as_of: date) -> tuple[date, ...]:
    with closing(connect_read_only(path)) as connection:
        return read_price_calendar_basis(connection, as_of=as_of)[1]
