"""実績と予想の利益basisを選び、収益性を計算する。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from math import isfinite
from statistics import fmean

from baibai_engine.market.financial_documents import statement_basis
from baibai_engine.market.financial_periods import period_days
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.market.metric_quality import OperatingProfitSource
from baibai_engine.market.price_models import JQuantsAdjustmentFactorEvent, JQuantsDailyBar
from baibai_engine.valuation.metrics.capital import _normalize_summaries_to_asof_basis
from baibai_engine.valuation.metrics.dividends import (
    _latest_consecutive_values,
    _latest_fy_revisions,
)
from baibai_engine.valuation.metrics.models import TTMRules
from baibai_engine.valuation.metrics.periods import (
    _carry_forward,
    _latest_summary,
    _ttm_value,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class NormalizedProfitSignals:
    """Split-safe multi-FY earnings inputs for Normalized Earnings Power and calibration."""

    normalized_per_3fy: float | None
    normalized_per_5fy: float | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfitabilityLevelSignals:
    """Disclosure-date-filtered profitability levels used only by calibration panels."""

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
    ttm_rules: TTMRules,
) -> NormalizedProfitSignals:
    """Build the preregistered 3/5-FY EPS anchors without filling missing years.

    Only FY observations may establish the split-reference basis, including when a
    caller supplies a mixed stream. Each anchor averages full-year per-share earnings
    across years rather than summing them, so the years may carry different share counts.
    Fiscal labels cannot replace completed full-year periods. Ineligible years stay
    in the sequence and cannot be skipped in favor of older, convenient earnings.
    """
    available = sorted(
        (
            summary
            for summary in summaries
            if summary.fiscal_period == "FY" and summary.disclosed_at <= asof_date
        ),
        key=lambda item: item.disclosed_at,
    )
    normalized = _normalize_summaries_to_asof_basis(available, ticker_bars, asof_date)
    fy_rows = _latest_fy_revisions(normalized)
    latest_basis = statement_basis(fy_rows[-1]) if fy_rows else None
    eps_values = {
        row.fiscal_year_end: row.eps_ttm
        for row in fy_rows
        if row.fiscal_year_end is not None
        and row.eps_ttm is not None
        and row.period_end == row.fiscal_year_end
        and (days := period_days(row)) is not None
        and ttm_rules.full_year_min_days <= days <= ttm_rules.full_year_max_days
        and statement_basis(row) == latest_basis
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
    otherwise the current value. Returns None when NI, CFO, or current total assets
    is missing, or when the resulting denominator is nonpositive.
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
