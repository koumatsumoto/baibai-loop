"""profit for metrics."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from math import isfinite
from statistics import fmean

from baibai_engine.market.bars import JQuantsAdjustmentFactorEvent
from baibai_engine.screening.metrics.capital import _normalize_summaries_to_asof_basis
from baibai_engine.screening.metrics.dividends import (
    _latest_consecutive_values,
    _latest_fy_revisions,
)
from baibai_engine.screening.metrics.periods import _carry_forward, _latest_summary, _ttm_value
from baibai_engine.screening.providers.jquants import JQuantsDailyBar, JQuantsFinancialSummary
from baibai_engine.screening.rule_config import TTMRules
from baibai_engine.screening.schema import (
    OperatingProfitSource,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class NormalizedProfitSignals:
    """Split-safe multi-FY earnings inputs for Normalized Earnings Power and calibration."""

    normalized_per_3fy: float | None
    normalized_per_5fy: float | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfitabilityLevelSignals:
    """PIT profitability levels used only by calibration panels."""

    operating_profit_to_assets: float | None
    operating_margin: float | None
    asset_turnover: float | None


def build_profitability_level_signals(
    summaries: Sequence[JQuantsFinancialSummary],
    asof_date: date,
    ttm_rules: TTMRules,
) -> ProfitabilityLevelSignals:
    """Build TTM profitability levels without profit-basis fallback or future rows."""

    available = tuple(
        summary
        for summary in sorted(summaries, key=lambda item: item.disclosed_at)
        if summary.disclosed_at <= asof_date
    )
    latest = _latest_summary(available)
    operating_profit_ttm, _ = _ttm_value(available, "operating_profit", ttm_rules)
    sales_ttm, _ = _ttm_value(available, "sales", ttm_rules)
    total_assets, _ = _carry_forward(available, "total_assets", latest)

    def ratio(numerator: float | None, denominator: float | None) -> float | None:
        if numerator is None or denominator is None or denominator <= 0:
            return None
        return numerator / denominator

    return ProfitabilityLevelSignals(
        operating_profit_to_assets=ratio(operating_profit_ttm, total_assets),
        operating_margin=ratio(operating_profit_ttm, sales_ttm),
        asset_turnover=ratio(sales_ttm, total_assets),
    )


def build_normalized_profit_signals(
    summaries: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    asof_date: date,
    *,
    close: float | None,
) -> NormalizedProfitSignals:
    """Build the preregistered 3/5-FY EPS anchors without filling missing years.

    Each anchor averages full-year per-share earnings across years, which is a mean of
    per-share values rather than a sum, so the years may carry different share counts.
    """
    available = sorted(
        (summary for summary in summaries if summary.disclosed_at <= asof_date),
        key=lambda item: item.disclosed_at,
    )
    normalized = _normalize_summaries_to_asof_basis(available, ticker_bars, asof_date)
    fy_rows = _latest_fy_revisions(normalized)
    eps_values = {
        row.fiscal_year_end: row.eps_ttm
        for row in fy_rows
        if row.fiscal_year_end is not None and row.eps_ttm is not None
    }
    latest_three = _latest_consecutive_values(fy_rows, eps_values, count=3)
    latest_five = _latest_consecutive_values(fy_rows, eps_values, count=5)

    def normalized_per(values: tuple[float, ...] | None) -> float | None:
        if values is None or close is None or close <= 0:
            return None
        average = fmean(values)
        return close / average if isfinite(average) and average > 0 else None

    return NormalizedProfitSignals(
        normalized_per_3fy=normalized_per(latest_three),
        normalized_per_5fy=normalized_per(latest_five),
    )


@dataclass(frozen=True, slots=True)
class _EarningsForecast:
    """One target-period earnings forecast resolved from ordered source documents."""

    target_period_end: date
    source: JQuantsFinancialSummary
    eps: float | None
    profit: float | None
    ordinary_profit: float | None


def _resolve_earnings_forecast(
    summaries: Sequence[JQuantsFinancialSummary],
    latest_actual: JQuantsFinancialSummary | None,
) -> _EarningsForecast | None:
    """Resolve the selected forecast under the persisted date-only v23 contract.

    The store does not retain document identity or current/next target metadata.  The
    newest persisted row is therefore the only state that both direct and SQLite paths
    can reproduce.  Period-aware composition needs a lossless source shape and is not
    inferred from the collapsed row.
    """

    del latest_actual
    source = summaries[-1] if summaries else None
    if source is None:
        return None
    return _EarningsForecast(
        target_period_end=date.max,
        source=source,
        eps=source.forecast_eps,
        profit=source.forecast_profit,
        ordinary_profit=source.forecast_ordinary_profit,
    )


def _select_operating_profit(
    summary: JQuantsFinancialSummary | None,
) -> tuple[float | None, OperatingProfitSource]:
    if summary is None:
        return None, OperatingProfitSource.NULL
    if summary.operating_profit is not None:
        return summary.operating_profit, OperatingProfitSource.OPERATING_PROFIT
    if summary.ordinary_profit is not None:
        return summary.ordinary_profit, OperatingProfitSource.ORDINARY_PROFIT
    if summary.profit is not None:
        return summary.profit, OperatingProfitSource.PROFIT
    return None, OperatingProfitSource.NULL


def _accruals_to_assets(
    *,
    net_income: float | None,
    ocf_ttm: float | None,
    total_assets: float | None,
    prior_total_assets: float | None,
) -> float | None:
    """Sloan (1996) accruals ratio: (NI - CFO) / average total assets.

    NI is the reported profit line composed to TTM, not a product of a per-share
    figure and a share count: EPS is per average share excluding treasury while the
    issued count includes it, so the product describes neither. The denominator uses
    the average of current and prior-year total assets when both are present,
    otherwise the current value. Returns None for any missing input or zero
    denominator.
    """
    if net_income is None or ocf_ttm is None or total_assets is None:
        return None
    denominator = (
        (total_assets + prior_total_assets) / 2.0
        if prior_total_assets is not None and prior_total_assets > 0
        else total_assets
    )
    if denominator <= 0:
        return None
    return (net_income - ocf_ttm) / denominator


def _loss_narrowing(current: float | None, previous: float | None) -> bool | None:
    if current is None or previous is None:
        return None
    if current >= 0:
        return False
    return previous < 0 and current > previous
