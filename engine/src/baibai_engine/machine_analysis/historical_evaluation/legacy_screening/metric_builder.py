"""旧較正replay用のFinancialSnapshotと比較指標を組み立てる。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.history import (
    VALUATION_HISTORY_SESSIONS,
    build_valuation_history,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.relative import (
    VALUATION_METRICS,
    relative_valuation,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.rule_config import (
    ScreeningRules,
    load_screening_rules,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.schema import (
    DerivedMetrics,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.financial_snapshot import (
    build_financial_snapshot,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    FinancialSnapshot,
)
from baibai_engine.machine_analysis.models.market_context.history import (
    PRICE_HISTORY_WINDOW_DAYS,
    avg_daily_volume,
    gap_from_low,
    latest_bar_on_or_before,
    price_change,
    realized_volatility,
)
from baibai_engine.machine_analysis.models.market_context.margin import (
    MarginBalance,
    margin_supply_demand,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EdinetMetricRecord
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.prices.price_basis import asof_basis_closes
from baibai_engine.machine_analysis.observations.prices.price_models import (
    JQuantsAdjustmentFactorEvent,
    JQuantsDailyBar,
)
from baibai_engine.machine_analysis.observations.stocks.models import SecurityMaster


@dataclass(frozen=True)
class MetricBuildResult:
    financials: Mapping[str, FinancialSnapshot]
    derived: Mapping[str, DerivedMetrics]


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
    medians to the common-eligible comparison population.
    Metrics are still computed for every ticker in
    ``securities_by_ticker``. This keeps relative-valuation judgments anchored
    to investable comparables even though the screen covers all common stocks;
    ``None`` uses the full scope as the population.
    """
    rules = rules or load_screening_rules()
    financials: dict[str, FinancialSnapshot] = {}

    for ticker in securities_by_ticker:
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
        # 価格はここでasofを選び、財務開示のasof選択はFinancialSnapshotが所有する。
        # 提供元の後日訂正を、過去のAPI取得状態へ戻す契約ではない。
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

        derived[ticker] = DerivedMetrics(
            sector_median_gap=relative.sector_median_gap,
            sector_median_value=relative.sector_median_value,
            sector_median_basis=relative.sector_median_basis,
            self_range_position=relative.self_range_position,
            self_range_median=relative.self_range_median,
            price_change_60d=price_change(
                ticker_bars, 60, asof_date, adjustment_events=adjustment_events
            ),
            realized_volatility_60d=realized_volatility(
                ticker_bars, 60, asof_date, adjustment_events=adjustment_events
            ),
            gap_from_52w_low=gap_from_low(
                ticker_bars, 252, asof_date, adjustment_events=adjustment_events
            ),
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

    return MetricBuildResult(financials=financials, derived=derived)


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
