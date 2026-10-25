"""共有読取された価格からテクニカル分析を生成する。取得・保存をしない。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from baibai_engine.data_collection.identifiers import normalize_ticker
from baibai_engine.market.price_models import PriceBar

from .calculations import (
    TECHNICAL_CALCULATION_REVISION,
    bollinger_20bar,
    rsi_14bar,
    simple_moving_average,
)


def generate_technical_analysis(
    bars: Sequence[PriceBar], *, ticker: str, as_of: date
) -> dict[str, object]:
    """同一調整基準の時系列を受け取り、個別読取と全社生成で同じ算術を使う。"""
    ticker = normalize_ticker(ticker)
    bars = sorted((bar for bar in bars if bar.traded_at <= as_of), key=lambda bar: bar.traded_at)
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
