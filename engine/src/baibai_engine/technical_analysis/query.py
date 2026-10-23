"""Read all retained prices once for one as-of Technical Analysis; never acquire or write."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from baibai_engine.collection.identifiers import normalize_ticker
from baibai_engine.market.adjusted_prices import read_price_bars

from .calculations import (
    TECHNICAL_CALCULATION_REVISION,
    bollinger_20bar,
    rsi_14bar,
    simple_moving_average,
)


def technical_analysis(path: Path, *, ticker: str, as_of: date) -> dict[str, object]:
    ticker = normalize_ticker(ticker)
    bars = read_price_bars(path, tickers=(ticker,), start=date.min, end=as_of).get(ticker, [])
    return {
        "ticker": ticker,
        "as_of": as_of.isoformat(),
        "calculation_revision": TECHNICAL_CALCULATION_REVISION,
        "price_basis": "price_adjusted_close",
        "history_start_date": bars[0].traded_at.isoformat() if bars else None,
        "history_end_date": bars[-1].traded_at.isoformat() if bars else None,
        "bar_count": len(bars),
        "read_semantics": "retained_prices_at_date_cutoff_not_original_publication_vintage",
        "moving_averages": {
            f"sma_{window}bar": simple_moving_average(bars, window_bars=window)
            for window in (20, 50, 200)
        },
        "bollinger_20bar": bollinger_20bar(bars),
        "rsi_14bar": rsi_14bar(bars),
    }
