"""Read all retained prices once for one as-of Technical Analysis; never acquire or write."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from baibai_engine.data_collection.identifiers import normalize_ticker
from baibai_engine.market.adjusted_prices import read_price_bars

from .generation import generate_technical_analysis


def technical_analysis(path: Path, *, ticker: str, as_of: date) -> dict[str, object]:
    ticker = normalize_ticker(ticker)
    bars = read_price_bars(path, tickers=(ticker,), start=date.min, end=as_of).get(ticker, [])
    return generate_technical_analysis(bars, ticker=ticker, as_of=as_of)
