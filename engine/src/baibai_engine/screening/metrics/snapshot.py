"""source factからFinancialSnapshotとScreeningMetricsを組み立てる。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from datetime import date, timedelta

from baibai_engine.market.bars import (
    JQuantsAdjustmentFactorEvent,
    JQuantsDailyBar,
    asof_basis_closes,
)
from baibai_engine.market.edinet_models import EdinetMetricRecord
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.market.metric_quality import TTMQuality
from baibai_engine.market.models import SecurityMaster
from baibai_engine.market_context.history import (
    PRICE_HISTORY_WINDOW_DAYS,
    avg_daily_volume,
    gap_from_low,
    latest_bar_on_or_before,
    price_change,
    realized_volatility,
    turnover_spike,
)
from baibai_engine.market_context.margin import (
    MarginBalance,
    margin_supply_demand,
)
from baibai_engine.market_context.sector import sector_relative_strength
from baibai_engine.screening.rule_config import ScreeningRules, load_screening_rules
from baibai_engine.screening.schema import DerivedMetrics
from baibai_engine.valuation.metrics.financial_snapshot import build_financial_snapshot
from baibai_engine.valuation.metrics.history import (
    VALUATION_HISTORY_SESSIONS,
    build_valuation_history,
)
from baibai_engine.valuation.metrics.models import FinancialSnapshot
from baibai_engine.valuation.metrics.relative import (
    VALUATION_METRICS,
    relative_valuation,
)

# metric 計算の入力窓 (暦日)。bars は 3 年自己レンジ percentile / sigma_gap に
# 1200 日、fin summaries は TTM 合成と前年同期 YoY に 730 日を要する。本番 run と
# 較正リプレイ (calibration/panel.py) が同じ値を import する。窓がずれると
# リプレイは本番と別物の指標を測るため、ここ以外に窓を定義しない。
# 期間の理由と適用先: docs/reference/screening-runtime.md#input-scope
BARS_INPUT_WINDOW_DAYS = 1200

FIN_INPUT_WINDOW_DAYS = 730

# 株主還元の変化と正規化PERは複数期の通期実績を必要とする。production の
# FinancialSnapshot / E[r] 入力窓は上の730日のまま維持し、正規化PERだけはFY行と
# split eventを疎に読む。全bar・全四半期を日次runへ載せないための別窓である。
SHAREHOLDER_RETURN_HISTORY_WINDOW_DAYS = 1200

NORMALIZED_EPS_HISTORY_WINDOW_DAYS = 2200


@dataclass(frozen=True)
class MetricBuildResult:
    financials: Mapping[str, FinancialSnapshot]
    derived: Mapping[str, DerivedMetrics]
    ttm_quality_counts: Mapping[str, int]
    yoy_missing_count: int


def build_metrics(
    asof_date: date,
    securities_by_ticker: Mapping[str, SecurityMaster],
    bars_by_ticker: Mapping[str, Sequence[JQuantsDailyBar]],
    summaries_by_ticker: Mapping[str, Sequence[JQuantsFinancialSummary]],
    edinet_by_ticker: Mapping[str, EdinetMetricRecord],
    rules: ScreeningRules | None = None,
    median_population: frozenset[str] | None = None,
    margin_latest: Mapping[str, MarginBalance] | None = None,
    margin_prior_26w: Mapping[str, MarginBalance] | None = None,
    valuation_history_sessions: int = VALUATION_HISTORY_SESSIONS,
    adjustment_events_by_ticker: Mapping[
        str, Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar]
    ]
    | None = None,
) -> MetricBuildResult:
    """Build per-ticker financial and derived metrics for the screen scope.

    ``margin_latest`` / ``margin_prior_26w`` carry the margin balances that were
    already published at ``asof_date``; leaving them out yields the same metrics
    with the supply/demand axes unset, which is what a store without a published
    margin balance produces.

    ``median_population`` restricts the comparison population for sector / market
    medians and sector relative strength to the common-eligible comparison population.
    Metrics are still computed for every ticker in
    ``securities_by_ticker``. This keeps relative-valuation judgments anchored
    to investable comparables even though the screen covers all common stocks;
    ``None`` uses the full scope as the population.
    """
    rules = rules or load_screening_rules()
    financials: dict[str, FinancialSnapshot] = {}
    latest_prices: dict[str, float] = {}

    for ticker, security in securities_by_ticker.items():
        del security
        latest_bar = latest_bar_on_or_before(bars_by_ticker.get(ticker, ()), asof_date)
        if latest_bar is None:
            continue
        ticker_bars = bars_by_ticker.get(ticker, ())
        adjustment_events = (
            adjustment_events_by_ticker.get(ticker, ())
            if adjustment_events_by_ticker is not None
            else ticker_bars
        )
        latest_price = asof_basis_closes([latest_bar], adjustment_events, asof_date=asof_date)[0]
        latest_prices[ticker] = latest_price
        # bar は `latest_bar_on_or_before` が asof で切る。開示行も同じ場所で切る。
        # 較正リプレイは過去の断面を作り直すので、asof より後の開示が 1 行混ざると
        # 「発表前の決算で割安に見える」行ができ、測ったすべての予測力が偽になる。
        # 呼び出し側が窓で切っている前提を置かない (本 module の他の 3 つの入口も
        # 同じ規律で自分で切っている)。
        financials[ticker] = build_financial_snapshot(
            latest_price=latest_price,
            summaries=summaries_by_ticker.get(ticker, ()),
            edinet=edinet_by_ticker.get(ticker),
            ttm_rules=rules.ttm,
            adjustment_events=adjustment_events,
            asof_date=asof_date,
        )

    def _in_population(ticker: str) -> bool:
        return median_population is None or ticker in median_population

    sector_metric_values: dict[str, dict[str, list[float]]] = {}
    for ticker, snapshot in financials.items():
        if not _in_population(ticker):
            continue
        sector = securities_by_ticker[ticker].sector_33
        sector_bucket = sector_metric_values.setdefault(
            sector, {metric: [] for metric in VALUATION_METRICS}
        )
        for metric in VALUATION_METRICS:
            value = getattr(snapshot, metric)
            if value is not None:
                sector_bucket[metric].append(value)

    market_metric_values = {
        metric: [
            getattr(snapshot, metric)
            for ticker, snapshot in financials.items()
            if _in_population(ticker) and getattr(snapshot, metric) is not None
        ]
        for metric in VALUATION_METRICS
    }

    ticker_returns_4w: dict[str, float] = {}
    for ticker in securities_by_ticker:
        ticker_bars = bars_by_ticker.get(ticker, ())
        adjustment_events = (
            adjustment_events_by_ticker.get(ticker, ())
            if adjustment_events_by_ticker is not None
            else ticker_bars
        )
        four_week = price_change(
            ticker_bars,
            20,
            asof_date,
            adjustment_events=adjustment_events,
        )
        if four_week is not None:
            ticker_returns_4w[ticker] = four_week

    sector_context = sector_relative_strength(
        {ticker: value for ticker, value in ticker_returns_4w.items() if _in_population(ticker)},
        {ticker: security.sector_33 for ticker, security in securities_by_ticker.items()},
    )
    sector_rs = sector_context.relative_returns
    rs_percentiles = sector_context.percentiles

    history_window_start = asof_date - timedelta(days=PRICE_HISTORY_WINDOW_DAYS)
    price_history_sessions: dict[str, int] = {
        ticker: sum(
            1
            for bar in bars_by_ticker.get(ticker, ())
            if history_window_start < bar.traded_at <= asof_date
        )
        for ticker in financials
    }
    # The densest ticker in scope approximates the full trading calendar for the
    # window, so coverage is a population-relative ratio with no calendar fetch.
    max_history_sessions = max(price_history_sessions.values(), default=0)

    derived: dict[str, DerivedMetrics] = {}
    for ticker, snapshot in financials.items():
        sector = securities_by_ticker[ticker].sector_33
        ticker_bars = bars_by_ticker.get(ticker, ())
        adjustment_events = (
            adjustment_events_by_ticker.get(ticker, ())
            if adjustment_events_by_ticker is not None
            else ticker_bars
        )
        valuation_history = build_valuation_history(
            latest_prices[ticker],
            ticker_bars,
            snapshot,
            asof_date,
            adjustment_events=adjustment_events,
            history_sessions=valuation_history_sessions,
        )
        relative = relative_valuation(
            snapshot,
            sector_metric_values.get(sector, {}),
            market_metric_values,
            valuation_history,
        )

        eligible_bars = sorted(
            (bar for bar in ticker_bars if bar.traded_at <= asof_date),
            key=lambda item: item.traded_at,
        )
        listing_span_days = (asof_date - eligible_bars[0].traded_at).days if eligible_bars else 0
        derived[ticker] = DerivedMetrics(
            sector_median_gap=relative.sector_median_gap,
            sector_median_value=relative.sector_median_value,
            sector_median_basis=relative.sector_median_basis,
            self_range_percentile=relative.self_range_percentile,
            self_range_median=relative.self_range_median,
            price_change_1d=price_change(
                ticker_bars, 1, asof_date, adjustment_events=adjustment_events
            ),
            price_change_5d=price_change(
                ticker_bars, 5, asof_date, adjustment_events=adjustment_events
            ),
            price_change_20d=price_change(
                ticker_bars, 20, asof_date, adjustment_events=adjustment_events
            ),
            price_change_60d=price_change(
                ticker_bars, 60, asof_date, adjustment_events=adjustment_events
            ),
            realized_volatility_60d=realized_volatility(
                ticker_bars, 60, asof_date, adjustment_events=adjustment_events
            ),
            gap_from_52w_low=gap_from_low(
                ticker_bars, 252, asof_date, adjustment_events=adjustment_events
            ),
            turnover_spike_5d=turnover_spike(ticker_bars, asof_date),
            sigma_gap=relative.sigma_gap,
            sector_relative_strength_4w=sector_rs.get(sector),
            sector_relative_strength_percentile=rs_percentiles.get(sector),
            ticker_return_4w=ticker_returns_4w.get(ticker),
            sector_return_4w=sector_context.mean_returns.get(sector),
            short_history_flag=listing_span_days < PRICE_HISTORY_WINDOW_DAYS,
            **asdict(
                margin_supply_demand(
                    latest=(margin_latest or {}).get(ticker),
                    prior_26w=(margin_prior_26w or {}).get(ticker),
                    avg_daily_volume_shares=avg_daily_volume(
                        ticker_bars, asof_date, adjustment_events=adjustment_events
                    ),
                    shares_outstanding=snapshot.shares_outstanding,
                    asof_date=asof_date,
                    adjustment_events=adjustment_events,
                )
            ),
            price_history_sessions_750d=price_history_sessions[ticker],
            price_history_coverage_750d=(
                price_history_sessions[ticker] / max_history_sessions
                if max_history_sessions > 0
                else None
            ),
        )

    ttm_quality_counts = _count_ttm_qualities(list(financials.values()))
    yoy_missing_count = sum(
        1
        for snapshot in financials.values()
        if snapshot.eps_yoy is None
        or snapshot.sales_yoy is None
        or snapshot.operating_profit_yoy is None
    )
    return MetricBuildResult(
        financials=financials,
        derived=derived,
        ttm_quality_counts=ttm_quality_counts,
        yoy_missing_count=yoy_missing_count,
    )


def group_adjustment_events_by_ticker(
    events: Sequence[JQuantsAdjustmentFactorEvent],
) -> dict[str, list[JQuantsAdjustmentFactorEvent]]:
    grouped: dict[str, list[JQuantsAdjustmentFactorEvent]] = {}
    for event in events:
        grouped.setdefault(event.ticker, []).append(event)
    for ticker in grouped:
        grouped[ticker].sort(key=lambda item: item.traded_at)
    return grouped


def group_bars_by_ticker(bars: Sequence[JQuantsDailyBar]) -> dict[str, list[JQuantsDailyBar]]:
    grouped: dict[str, list[JQuantsDailyBar]] = {}
    for bar in bars:
        grouped.setdefault(bar.ticker, []).append(bar)
    return grouped


def group_summaries_by_ticker(
    summaries: Sequence[JQuantsFinancialSummary],
) -> dict[str, list[JQuantsFinancialSummary]]:
    grouped: dict[str, list[JQuantsFinancialSummary]] = {}
    for summary in summaries:
        grouped.setdefault(summary.ticker, []).append(summary)
    for ticker in grouped:
        grouped[ticker].sort(key=lambda item: item.disclosed_at)
    return grouped


_TTM_QUALITY_FIELDS = tuple(
    field.name for field in fields(FinancialSnapshot) if field.name.startswith("ttm_quality_")
)


def _count_ttm_qualities(snapshots: Sequence[FinancialSnapshot]) -> dict[str, int]:
    # 集計対象は schema の ttm_quality_* field から導出する (次元を足したとき
    # ここの列挙更新漏れで集計 telemetry から欠落するのを防ぐ)。
    counts = {quality.value: 0 for quality in TTMQuality}
    for snapshot in snapshots:
        for field_name in _TTM_QUALITY_FIELDS:
            quality: TTMQuality | None = getattr(snapshot, field_name)
            if quality is not None:
                counts[quality.value] += 1
    return counts
