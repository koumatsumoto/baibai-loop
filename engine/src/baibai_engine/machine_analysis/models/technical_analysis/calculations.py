"""Pure price-based Technical Analysis, separate from valuation and trading decisions."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from itertools import pairwise

from baibai_engine.market.price_models import PriceBar

TECHNICAL_CALCULATION_REVISION = "sma20-50-200-bb20-wilder-rsi14-price-path-v2"


def bollinger_20bar(bars: Sequence[PriceBar]) -> dict[str, object]:
    """Describe the last 20 observed closes, not value or reversal probability."""
    window = bars[-20:]
    result: dict[str, object] = {
        "status": "insufficient_history",
        "window_bars": len(window),
        "window_start_date": window[0].traded_at.isoformat() if window else None,
        "window_end_date": window[-1].traded_at.isoformat() if window else None,
        "middle_band": None,
        "sigma": None,
        "upper_band": None,
        "lower_band": None,
        "bandwidth": None,
        "percent_b": None,
        "z_score": None,
        "position": None,
    }
    if len(window) < 20:
        return result
    prices = [bar.price for bar in window]
    if any(not math.isfinite(price) or price <= 0 for price in prices):
        return {**result, "status": "invalid_price"}
    mean = statistics.fmean(prices)
    sigma = statistics.pstdev(prices)
    upper, lower = mean + 2 * sigma, mean - 2 * sigma
    result.update(
        middle_band=mean,
        sigma=sigma,
        upper_band=upper,
        lower_band=lower,
        bandwidth=4 * sigma / mean,
    )
    if sigma == 0:
        return {**result, "status": "zero_variance"}
    current = prices[-1]
    return {
        **result,
        "status": "available",
        "percent_b": (current - lower) / (4 * sigma),
        "z_score": (current - mean) / sigma,
        "position": "above_upper"
        if current > upper
        else "below_lower"
        if current < lower
        else "within_bands",
    }


def rsi_14bar(bars: Sequence[PriceBar]) -> dict[str, object]:
    """Seed on the first 14 changes, then apply Wilder smoothing to the full prefix."""
    result: dict[str, object] = {
        "status": "insufficient_history",
        "period_bars": 14,
        "bar_count": len(bars),
        "history_start_date": bars[0].traded_at.isoformat() if bars else None,
        "history_end_date": bars[-1].traded_at.isoformat() if bars else None,
        "seed_end_date": bars[14].traded_at.isoformat() if len(bars) >= 15 else None,
        "average_gain": None,
        "average_loss": None,
        "value": None,
    }
    if any(not math.isfinite(bar.price) or bar.price <= 0 for bar in bars):
        return {**result, "status": "invalid_price"}
    if len(bars) < 15:
        return result
    changes = [current.price - previous.price for previous, current in pairwise(bars)]
    gain = statistics.fmean(max(change, 0.0) for change in changes[:14])
    loss = statistics.fmean(max(-change, 0.0) for change in changes[:14])
    for change in changes[14:]:
        gain = (13 * gain + max(change, 0.0)) / 14
        loss = (13 * loss + max(-change, 0.0)) / 14
    # Both-zero is an explicit convention, not a probability or a trading signal.
    value = 50.0 if gain == loss == 0 else 100.0 if loss == 0 else 100 * gain / (gain + loss)
    return {
        **result,
        "status": "available",
        "average_gain": gain,
        "average_loss": loss,
        "value": value,
    }


def simple_moving_average(bars: Sequence[PriceBar], *, window_bars: int) -> dict[str, object]:
    """A complete observed-bar SMA and close gap; the caller fixes the supported periods."""
    if window_bars < 1:
        raise ValueError("moving-average window must be positive")
    window = bars[-window_bars:]
    result: dict[str, object] = {
        "status": "insufficient_history",
        "window_bars": len(window),
        "window_start_date": window[0].traded_at.isoformat() if window else None,
        "window_end_date": window[-1].traded_at.isoformat() if window else None,
        "value": None,
        "gap_ratio": None,
    }
    if len(window) < window_bars:
        return result
    if any(not math.isfinite(bar.price) or bar.price <= 0 for bar in window):
        return {**result, "status": "invalid_price"}
    value = statistics.fmean(bar.price for bar in window)
    return {
        **result,
        "status": "available",
        "value": value,
        "gap_ratio": window[-1].price / value - 1,
    }
