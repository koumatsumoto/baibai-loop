"""Produce a deterministic benchmark trend for market and ticker fact profiles.

The fixed 20-session thresholds describe only the Nikkei 225 ETF proxy's price
direction. The resulting fact-layer annotation never gates or re-ranks the Review Set.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path

from .history import trailing_return
from .prices import read_price_bars

NIKKEI225_ETF_PROXY = "1321"

# Pre-registered fixed thresholds (no grid search; docs/reference/market-context.md).
# +-3% over 20 business days is roughly a +-40% annualized drift, a conventional
# bar for calling a directional move rather than range noise.
RALLY_RETURN_20D_MIN = 0.03
SELLOFF_RETURN_20D_MAX = -0.03

TREND_WINDOW_BARS = 20
LONG_TREND_WINDOW_BARS = 60

# ``market_snapshot`` shares these windows so its weekly fact history stays aligned.
BREADTH_MA_WINDOW_BARS = 20
DEFAULT_MIN_BREADTH_SAMPLE = 100

# 52 trading-day weeks for the benchmark gap-from-high, matching the per-ticker
# 52w convention in ticker_profile. The trend windows above read only the tail of
# the series, so loading the longer window does not change the 20d/60d returns.
_WEEK_52_WINDOW_BARS = 252
# Load enough calendar history to cover _WEEK_52_WINDOW_BARS trading days for the
# 52-week high (252 sessions ~ 353 calendar days; add buffer for holidays).
_BENCHMARK_LOOKBACK_CALENDAR_DAYS = 400


class BenchmarkTrendState(StrEnum):
    UPTREND = "uptrend"
    DOWNTREND = "downtrend"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BenchmarkTrendSnapshot:
    as_of: date
    benchmark_ticker: str
    observation_date: date
    benchmark_return_20d: float | None
    benchmark_return_60d: float | None
    # Benchmark gap below its high over the lookback window (<=0; 0 = at the high).
    benchmark_gap_from_high: float | None
    trend_state: BenchmarkTrendState

    def to_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "benchmark_ticker": self.benchmark_ticker,
            "observation_date": self.observation_date.isoformat(),
            "benchmark_return_20d": self.benchmark_return_20d,
            "benchmark_return_60d": self.benchmark_return_60d,
            "benchmark_gap_from_high": self.benchmark_gap_from_high,
            "trend_state": self.trend_state.value,
        }


def classify_benchmark_trend(benchmark_return_20d: float | None) -> BenchmarkTrendState:
    """Classify the benchmark direction from fixed thresholds."""
    if benchmark_return_20d is None:
        return BenchmarkTrendState.UNKNOWN
    if benchmark_return_20d >= RALLY_RETURN_20D_MIN:
        return BenchmarkTrendState.UPTREND
    if benchmark_return_20d <= SELLOFF_RETURN_20D_MAX:
        return BenchmarkTrendState.DOWNTREND
    return BenchmarkTrendState.NEUTRAL


def compute_benchmark_trend(
    sqlite_path: Path,
    asof_date: date,
    *,
    benchmark_ticker: str = NIKKEI225_ETF_PROXY,
) -> BenchmarkTrendSnapshot | None:
    """Compute the benchmark trend snapshot from cached daily bars.

    Returns ``None`` when the SQLite cache is absent or holds no bars on or
    before ``asof_date``; insufficient history degrades individual fields to
    ``None`` and the state to ``unknown`` instead of failing, so callers can
    keep ranking with the diagnostic disabled while recording why.
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
    observation_date = benchmark_series[-1][0]
    benchmark_return_20d = trailing_return(
        [price for _, price in benchmark_series], TREND_WINDOW_BARS
    )
    benchmark_return_60d = trailing_return(
        [price for _, price in benchmark_series], LONG_TREND_WINDOW_BARS
    )
    return BenchmarkTrendSnapshot(
        as_of=asof_date,
        benchmark_ticker=benchmark_ticker,
        observation_date=observation_date,
        benchmark_return_20d=benchmark_return_20d,
        benchmark_return_60d=benchmark_return_60d,
        benchmark_gap_from_high=_gap_from_high(benchmark_series),
        trend_state=classify_benchmark_trend(benchmark_return_20d),
    )


def _load_benchmark_series(
    sqlite_path: Path,
    asof_date: date,
    *,
    ticker: str,
) -> list[tuple[date, float]]:
    bars = read_price_bars(
        sqlite_path,
        tickers=(ticker,),
        start=asof_date - timedelta(days=_BENCHMARK_LOOKBACK_CALENDAR_DAYS),
        end=asof_date,
    ).get(ticker, [])
    return [(bar.traded_at, bar.price) for bar in bars]


def _gap_from_high(series: list[tuple[date, float]]) -> float | None:
    """Current close vs the highest close over the trailing 52 weeks (<=0).

    0 means the benchmark sits at its 52-week high (最高値圏). Using the 52-week
    window (not just the recent few months) avoids a false "at the high" read when
    the index is near a recent local high but still below its 52-week high.
    """
    if not series:
        return None
    window = series[-_WEEK_52_WINDOW_BARS:]
    high = max(price for _, price in window)
    if high <= 0:
        return None
    return window[-1][1] / high - 1
