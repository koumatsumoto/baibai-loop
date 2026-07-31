"""Label an as-of with the market state the TOPIX close series was in.

Written for one measurement — whether E[r] realises differently depending on what
the market was doing — and kept here rather than in the engine because nothing
reads it. It is not a successor to `screening.regime`, which answers a different
question with a different vocabulary and is wired into the snapshot commands; the
two are unrelated.

Definitions and thresholds are fixed in
reports/2026-07-31-market-regime-v2-preregistration.md and are not tuned here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from math import log, sqrt
from statistics import fmean, stdev

# Trading days, not calendar days: the index and the equity store share one
# calendar, so a window in bars is the same window on both sides.
DRAWDOWN_WINDOW_BARS = 252
RETURN_WINDOW_BARS = 60
VOLATILITY_WINDOW_BARS = 60
VOLATILITY_PERCENTILE_WINDOW_BARS = 1250
TRADING_DAYS_PER_YEAR = 252

STRESS_DRAWDOWN = -0.15
RECOVERY_DRAWDOWN = -0.08
EXTENDED_DRAWDOWN = -0.03
EXTENDED_VOLATILITY_PERCENTILE = 0.5

REGIMES: tuple[str, ...] = ("stress", "recovery", "extended", "normal", "unknown")


@dataclass(frozen=True, slots=True, kw_only=True)
class RegimeReading:
    """One as-of's market state and the three quantities that decided it."""

    asof: date
    regime: str
    drawdown: float | None
    return_60d: float | None
    volatility_percentile: float | None


def regime_at(closes: Sequence[tuple[date, float]], asof: date) -> RegimeReading:
    """Label `asof` from the index closes at or before it.

    Every input is drawn from closes up to `asof`, so the label is one that could
    have been read on the day. Too little history degrades a quantity to None and
    the label to `unknown` rather than guessing, because a cohort whose regime is
    unknown must be visible as such rather than absorbed into `normal`.
    """
    history = [(day, close) for day, close in closes if day <= asof and close > 0]
    history.sort()
    if not history:
        return RegimeReading(
            asof=asof, regime="unknown", drawdown=None, return_60d=None, volatility_percentile=None
        )
    values = [close for _, close in history]
    drawdown = _drawdown(values)
    return_60d = _trailing_return(values)
    volatility_percentile = _volatility_percentile(values)
    return RegimeReading(
        asof=asof,
        regime=_classify(drawdown, return_60d, volatility_percentile),
        drawdown=drawdown,
        return_60d=return_60d,
        volatility_percentile=volatility_percentile,
    )


def _classify(
    drawdown: float | None, return_60d: float | None, volatility_percentile: float | None
) -> str:
    if drawdown is None:
        return "unknown"
    if drawdown <= STRESS_DRAWDOWN:
        return "stress"
    if drawdown <= RECOVERY_DRAWDOWN and return_60d is not None and return_60d > 0:
        return "recovery"
    if (
        drawdown > EXTENDED_DRAWDOWN
        and volatility_percentile is not None
        and volatility_percentile < EXTENDED_VOLATILITY_PERCENTILE
    ):
        return "extended"
    return "normal"


def _drawdown(values: Sequence[float]) -> float | None:
    if len(values) < DRAWDOWN_WINDOW_BARS:
        return None
    window = values[-DRAWDOWN_WINDOW_BARS:]
    peak = max(window)
    return (window[-1] / peak) - 1.0 if peak > 0 else None


def _trailing_return(values: Sequence[float]) -> float | None:
    if len(values) <= RETURN_WINDOW_BARS:
        return None
    start = values[-(RETURN_WINDOW_BARS + 1)]
    return (values[-1] / start) - 1.0 if start > 0 else None


def _realised_volatility(values: Sequence[float]) -> float | None:
    if len(values) <= VOLATILITY_WINDOW_BARS:
        return None
    window = values[-(VOLATILITY_WINDOW_BARS + 1) :]
    returns = [log(later / earlier) for earlier, later in pairwise(window)]
    if len(returns) < 2:
        return None
    return stdev(returns) * sqrt(TRADING_DAYS_PER_YEAR)


def _volatility_percentile(values: Sequence[float]) -> float | None:
    """Where today's realised volatility sits inside the last five years of it.

    A level is not comparable across a decade — the same annualised figure is calm
    in one period and turbulent in another — so the state reads the rank rather
    than the number.
    """
    current = _realised_volatility(values)
    if current is None:
        return None
    history: list[float] = []
    span = min(len(values), VOLATILITY_PERCENTILE_WINDOW_BARS)
    # Each sample needs a full volatility window behind it, so the first end that
    # can produce one is the window length plus the return that starts it.
    first_end = max(len(values) - span + 1, VOLATILITY_WINDOW_BARS + 1)
    for end in range(first_end, len(values) + 1):
        measured = _realised_volatility(values[end - VOLATILITY_WINDOW_BARS - 1 : end])
        if measured is not None:
            history.append(measured)
    if len(history) < VOLATILITY_WINDOW_BARS:
        return None
    return fmean(1.0 if measured <= current else 0.0 for measured in history)
