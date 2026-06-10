"""Market regime snapshot derived mechanically from cached daily bars.

The 2026-05 replay showed the oversold/value tilt structurally lagging the
benchmark while it rallied (4w relative -5.6 to -12.3pt across all profiles).
This module classifies the market regime from price facts only (benchmark
trend) so selection can stop boosting fast-dislocation candidates while the
index is trending up. The snapshot is a fact-layer artifact: thresholds are
fixed up front and never fitted to past data, and the label feeds a ranking
lens, not a hard gate.

The classification is trend-only by design. Fast-dislocation picks
anti-momentum names (recent heavy decliners); when benchmark momentum is
strongly positive those laggards mechanically underperform whether the rally
is broad or narrow — 2026-05 itself was a narrow rally (benchmark +8.7 to
+17.4% over 20 bars with breadth below 45%). Breadth is still computed and
recorded
as a fact field for diagnostics and future refinement, but it does not gate
the label.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path

NIKKEI225_ETF_PROXY = "1321"

# Pre-registered fixed thresholds (no grid search; see docs/screening/mechanical.md).
# +-3% over 20 business days is roughly a +-40% annualized drift, a conventional
# bar for calling a directional move rather than range noise.
RALLY_RETURN_20D_MIN = 0.03
SELLOFF_RETURN_20D_MAX = -0.03

TREND_WINDOW_BARS = 20
LONG_TREND_WINDOW_BARS = 60
BREADTH_MA_WINDOW_BARS = 20
DEFAULT_MIN_BREADTH_SAMPLE = 100

_BREADTH_LOOKBACK_CALENDAR_DAYS = 60
_BENCHMARK_LOOKBACK_CALENDAR_DAYS = 150


class MarketRegime(StrEnum):
    RISK_ON_RALLY = "risk_on_rally"
    RISK_OFF_SELLOFF = "risk_off_selloff"
    NEUTRAL_RANGE = "neutral_range"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class MarketRegimeSnapshot:
    asof: date
    benchmark_ticker: str
    eval_date: date
    benchmark_return_20d: float | None
    benchmark_return_60d: float | None
    breadth_pct_above_ma20: float | None
    breadth_sample_size: int
    regime: MarketRegime

    def to_dict(self) -> dict[str, object]:
        return {
            "asof": self.asof.isoformat(),
            "benchmark_ticker": self.benchmark_ticker,
            "eval_date": self.eval_date.isoformat(),
            "benchmark_return_20d": self.benchmark_return_20d,
            "benchmark_return_60d": self.benchmark_return_60d,
            "breadth_pct_above_ma20": self.breadth_pct_above_ma20,
            "breadth_sample_size": self.breadth_sample_size,
            "regime": self.regime.value,
        }


def classify_market_regime(benchmark_return_20d: float | None) -> MarketRegime:
    """Classify the regime from the fixed trend threshold; unknown when missing."""
    if benchmark_return_20d is None:
        return MarketRegime.UNKNOWN
    if benchmark_return_20d >= RALLY_RETURN_20D_MIN:
        return MarketRegime.RISK_ON_RALLY
    if benchmark_return_20d <= SELLOFF_RETURN_20D_MAX:
        return MarketRegime.RISK_OFF_SELLOFF
    return MarketRegime.NEUTRAL_RANGE


def compute_market_regime(
    sqlite_path: Path,
    asof_date: date,
    *,
    benchmark_ticker: str = NIKKEI225_ETF_PROXY,
    min_breadth_sample: int = DEFAULT_MIN_BREADTH_SAMPLE,
) -> MarketRegimeSnapshot | None:
    """Compute the regime snapshot from cached daily bars as of ``asof_date``.

    Returns ``None`` when the SQLite cache is absent or holds no bars on or
    before ``asof_date``; insufficient history degrades individual fields to
    ``None`` and the regime to ``unknown`` instead of failing, so callers can
    keep ranking with the lens disabled while recording why.
    """
    if not sqlite_path.exists():
        return None
    benchmark_series = _load_close_series(
        sqlite_path,
        asof_date,
        lookback_days=_BENCHMARK_LOOKBACK_CALENDAR_DAYS,
        ticker=benchmark_ticker,
    ).get(benchmark_ticker, [])
    breadth_series = _load_close_series(
        sqlite_path,
        asof_date,
        lookback_days=_BREADTH_LOOKBACK_CALENDAR_DAYS,
    )
    eval_date = _latest_traded_date(benchmark_series, breadth_series)
    if eval_date is None:
        return None
    benchmark_return_20d = _trailing_return(benchmark_series, TREND_WINDOW_BARS)
    benchmark_return_60d = _trailing_return(benchmark_series, LONG_TREND_WINDOW_BARS)
    breadth, sample_size = _breadth_above_ma(
        breadth_series,
        eval_date,
        min_sample=min_breadth_sample,
    )
    return MarketRegimeSnapshot(
        asof=asof_date,
        benchmark_ticker=benchmark_ticker,
        eval_date=eval_date,
        benchmark_return_20d=benchmark_return_20d,
        benchmark_return_60d=benchmark_return_60d,
        breadth_pct_above_ma20=breadth,
        breadth_sample_size=sample_size,
        regime=classify_market_regime(benchmark_return_20d),
    )


def _load_close_series(
    sqlite_path: Path,
    asof_date: date,
    *,
    lookback_days: int,
    ticker: str | None = None,
) -> dict[str, list[tuple[date, float]]]:
    start = asof_date - timedelta(days=lookback_days)
    query = (
        "SELECT ticker, traded_at, close, adjustment_close FROM jquants_daily_bars "
        "WHERE traded_at >= ? AND traded_at <= ?"
    )
    params: tuple[object, ...] = (start.isoformat(), asof_date.isoformat())
    if ticker is not None:
        query += " AND ticker = ?"
        params = (*params, ticker)
    query += " ORDER BY ticker, traded_at"
    conn = sqlite3.connect(sqlite_path)
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    series: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for row_ticker, traded_at, close, adjustment_close in rows:
        price = adjustment_close if adjustment_close is not None else close
        if price is None or traded_at is None:
            continue
        series[str(row_ticker)].append((date.fromisoformat(traded_at), float(price)))
    return dict(series)


def _latest_traded_date(
    benchmark_series: list[tuple[date, float]],
    breadth_series: dict[str, list[tuple[date, float]]],
) -> date | None:
    dates = [traded_at for traded_at, _ in benchmark_series]
    dates.extend(traded_at for series in breadth_series.values() for traded_at, _ in series)
    return max(dates, default=None)


def _trailing_return(series: list[tuple[date, float]], window_bars: int) -> float | None:
    if len(series) < window_bars + 1:
        return None
    current = series[-1][1]
    past = series[-(window_bars + 1)][1]
    if past == 0:
        return None
    return current / past - 1


def _breadth_above_ma(
    breadth_series: dict[str, list[tuple[date, float]]],
    eval_date: date,
    *,
    min_sample: int,
) -> tuple[float | None, int]:
    above = 0
    sample = 0
    for series in breadth_series.values():
        if len(series) < BREADTH_MA_WINDOW_BARS or series[-1][0] != eval_date:
            continue
        window = [price for _, price in series[-BREADTH_MA_WINDOW_BARS:]]
        moving_average = sum(window) / len(window)
        sample += 1
        if series[-1][1] > moving_average:
            above += 1
    if sample < min_sample:
        return None, sample
    return above / sample, sample
