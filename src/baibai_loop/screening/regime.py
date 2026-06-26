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
+17.4% over 20 bars). Breadth was previously recorded as a diagnostic field
but did not gate the label, so it was removed in cleanup round 2.
"""

from __future__ import annotations

import sqlite3
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

# market_snapshot uses these for the weekly history packet (fact layer only,
# never gates the label). They live here so market_snapshot stays in sync with
# the regime classifier window.
BREADTH_MA_WINDOW_BARS = 20
DEFAULT_MIN_BREADTH_SAMPLE = 100

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
    # Benchmark gap below its high over the regime lookback window (<=0; 0 = at
    # the high). A fact-layer field, never gates the label. When this is ~0 with
    # regime=risk_on_rally the index is extended near its highs (最高値圏): risk-
    # reward judgement must then stress the downside (tail/gap), not read the
    # trailing target/stop ratio as the real RR.
    benchmark_gap_from_high: float | None
    regime: MarketRegime

    def to_dict(self) -> dict[str, object]:
        return {
            "asof": self.asof.isoformat(),
            "benchmark_ticker": self.benchmark_ticker,
            "eval_date": self.eval_date.isoformat(),
            "benchmark_return_20d": self.benchmark_return_20d,
            "benchmark_return_60d": self.benchmark_return_60d,
            "benchmark_gap_from_high": self.benchmark_gap_from_high,
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
) -> MarketRegimeSnapshot | None:
    """Compute the regime snapshot from cached daily bars as of ``asof_date``.

    Returns ``None`` when the SQLite cache is absent or holds no bars on or
    before ``asof_date``; insufficient history degrades individual fields to
    ``None`` and the regime to ``unknown`` instead of failing, so callers can
    keep ranking with the lens disabled while recording why.
    """
    if not sqlite_path.exists():
        return None
    benchmark_series = _load_benchmark_series(
        sqlite_path,
        asof_date,
        ticker=benchmark_ticker,
    )
    if not benchmark_series:
        return None
    eval_date = benchmark_series[-1][0]
    benchmark_return_20d = _trailing_return(benchmark_series, TREND_WINDOW_BARS)
    benchmark_return_60d = _trailing_return(benchmark_series, LONG_TREND_WINDOW_BARS)
    return MarketRegimeSnapshot(
        asof=asof_date,
        benchmark_ticker=benchmark_ticker,
        eval_date=eval_date,
        benchmark_return_20d=benchmark_return_20d,
        benchmark_return_60d=benchmark_return_60d,
        benchmark_gap_from_high=_gap_from_high(benchmark_series),
        regime=classify_market_regime(benchmark_return_20d),
    )


def _load_benchmark_series(
    sqlite_path: Path,
    asof_date: date,
    *,
    ticker: str,
) -> list[tuple[date, float]]:
    start = asof_date - timedelta(days=_BENCHMARK_LOOKBACK_CALENDAR_DAYS)
    query = (
        "SELECT traded_at, close, adjustment_close FROM jquants_daily_bars "
        "WHERE ticker = ? AND traded_at >= ? AND traded_at <= ? ORDER BY traded_at"
    )
    conn = sqlite3.connect(sqlite_path)
    try:
        rows = conn.execute(query, (ticker, start.isoformat(), asof_date.isoformat())).fetchall()
    finally:
        conn.close()
    series: list[tuple[date, float]] = []
    for traded_at, close, adjustment_close in rows:
        price = adjustment_close if adjustment_close is not None else close
        if price is None or traded_at is None:
            continue
        series.append((date.fromisoformat(traded_at), float(price)))
    return series


def _trailing_return(series: list[tuple[date, float]], window_bars: int) -> float | None:
    if len(series) < window_bars + 1:
        return None
    current = series[-1][1]
    past = series[-(window_bars + 1)][1]
    if past == 0:
        return None
    return current / past - 1


def _gap_from_high(series: list[tuple[date, float]]) -> float | None:
    """Current close vs the highest close over the loaded window (<=0).

    0 means the benchmark sits at its window high (最高値圏). Computed over the
    same ~150-calendar-day lookback the trend windows use, so it reads "near a
    recent high", not strictly an all-time high.
    """
    if not series:
        return None
    high = max(price for _, price in series)
    if high <= 0:
        return None
    return series[-1][1] / high - 1
