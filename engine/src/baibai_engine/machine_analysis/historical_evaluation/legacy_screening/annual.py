"""旧較正の連続FY・正規化利益・収益性・還元変化。現行候補政策ではない。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from itertools import pairwise
from math import isfinite
from statistics import fmean

from baibai_engine.machine_analysis.models.company_financials.metrics.capital import (
    _normalize_summaries_to_asof_basis,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.dividends import (
    _dividend_basis_requires_resolution,
    _forecast_annual_dps,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import TTMRules
from baibai_engine.machine_analysis.models.company_financials.metrics.periods import (
    _carry_forward,
    _latest_actual_row,
    _latest_summary,
    _ttm_components,
    _ttm_value,
)
from baibai_engine.machine_analysis.observations.financials.financial_documents import (
    accounting_period_identity,
    accounting_standard,
    comparable_reporting_basis,
    latest_disclosure_candidates,
    latest_reporting_basis_matches,
    project_financial_documents,
    statement_basis,
)
from baibai_engine.machine_analysis.observations.financials.financial_periods import (
    is_annual_period,
    period_days,
    resolve_actual_conflicts,
)
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.prices.price_models import (
    JQuantsAdjustmentFactorEvent,
    JQuantsDailyBar,
)


@dataclass(frozen=True, slots=True)
class ShareholderReturnChangeSignals:
    """Disclosure-date-filtered facts used only by the calibration panel."""

    dps_streak_up: bool | None
    dps_yoy_latest: float | None
    dps_guidance_up: bool | None
    dividend_initiation: bool | None
    share_count_reduction_streak: int | None
    shareholder_return_change: bool | None


def build_shareholder_return_change_signals(
    summaries: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    asof_date: date,
) -> ShareholderReturnChangeSignals:
    """Build preregistered shareholder-return change facts for calibration.

    Revisions are resolved before field availability is inspected: a null in the
    newest revision cannot silently fall back to an older value. DPS comparability
    is resolved separately from normalized gross share counts. The latter is
    deliberately described as share-count reduction rather than proof of a
    buyback because the source includes treasury stock.
    """
    available = project_financial_documents(summaries, asof_date)
    normalized = _normalize_summaries_to_asof_basis(available, ticker_bars, asof_date)
    fy_rows = _latest_fy_revisions(normalized)

    dps_values = _asof_basis_fy_dps(fy_rows, ticker_bars, asof_date)
    latest_pair = _latest_consecutive_values(fy_rows, dps_values, count=2, annual=True)
    latest_three = _latest_consecutive_values(fy_rows, dps_values, count=3, annual=True)

    dps_streak_up = (
        latest_three[0] <= latest_three[1] <= latest_three[2] if latest_three is not None else None
    )
    actual_up: bool | None = None
    dps_yoy_latest: float | None = None
    if latest_pair is not None:
        prior_dps, latest_dps = latest_pair
        actual_up = latest_dps > prior_dps
        if prior_dps > 0:
            dps_yoy_latest = (latest_dps / prior_dps) - 1.0
        elif latest_dps == 0:
            dps_yoy_latest = 0.0

    latest_actual_row = _latest_row_with_value(fy_rows, dps_values)
    if latest_actual_row is not None:
        assert latest_actual_row.fiscal_year_end is not None
        latest_actual = dps_values[latest_actual_row.fiscal_year_end]
    else:
        latest_actual = None
    forecast = _forecast_annual_dps(normalized)
    dps_guidance_up = (
        forecast > latest_actual if forecast is not None and latest_actual is not None else None
    )
    dividend_initiation = _dividend_initiation(
        latest_pair=latest_pair,
        latest_actual=latest_actual,
        forecast=forecast,
    )

    share_values = {
        row.fiscal_year_end: row.shares_outstanding
        for row in fy_rows
        if row.fiscal_year_end is not None
        and row.shares_outstanding is not None
        and row.shares_outstanding > 0
    }
    latest_share_three = _latest_consecutive_values(fy_rows, share_values, count=3, annual=False)
    share_count_reduction_streak: int | None = None
    if latest_share_three is not None:
        share_count_reduction_streak = 0
        for prior, current in reversed(tuple(pairwise(latest_share_three))):
            if current >= prior:
                break
            share_count_reduction_streak += 1

    change_components = (
        actual_up,
        dps_guidance_up,
        dividend_initiation,
        (share_count_reduction_streak >= 1 if share_count_reduction_streak is not None else None),
    )
    if any(value is True for value in change_components):
        shareholder_return_change: bool | None = True
    elif all(value is False for value in change_components):
        shareholder_return_change = False
    else:
        shareholder_return_change = None

    return ShareholderReturnChangeSignals(
        dps_streak_up=dps_streak_up,
        dps_yoy_latest=dps_yoy_latest,
        dps_guidance_up=dps_guidance_up,
        dividend_initiation=dividend_initiation,
        share_count_reduction_streak=share_count_reduction_streak,
        shareholder_return_change=shareholder_return_change,
    )


def _latest_fy_revisions(
    summaries: Sequence[JQuantsFinancialSummary],
) -> list[JQuantsFinancialSummary]:
    grouped: dict[date, list[JQuantsFinancialSummary]] = {}
    for summary in resolve_actual_conflicts(summaries):
        if summary.fiscal_period != "FY" or summary.fiscal_year_end is None:
            continue
        if summary.document_type in {"EarnForecastRevision", "DividendForecastRevision"}:
            continue
        grouped.setdefault(summary.fiscal_year_end, []).append(summary)
    result = []
    for year_end in sorted(grouped):
        rows = grouped[year_end]
        tied = latest_disclosure_candidates(rows)
        # Preserve unresolved years in the consecutive-FY series rather than
        # silently skipping a contradictory EPS/DPS observation.
        representative = max(
            tied,
            key=lambda row: (
                sum(
                    getattr(row, field) is not None
                    for field in ("shares_outstanding", "treasury_shares")
                ),
                row.cumulative_eps is not None,
                row.dps_actual_annual is not None,
            ),
        )
        if len(tied) == 1:
            result.append(representative)
            continue
        if len({accounting_period_identity(row) for row in tied}) != 1:
            # A shared FY label does not resolve conflicting reported periods.
            # Keep the year in the series, but never let input order choose its
            # earnings, payout window or capital observation. Zero DPS is unit-free.
            result.append(
                replace(
                    representative,
                    period_start=None,
                    period_end=None,
                    cumulative_eps=None,
                    dps_actual_annual=0.0
                    if all(row.dps_actual_annual == 0 for row in tied)
                    else None,
                    shares_outstanding=None,
                    treasury_shares=None,
                    disclosure_number=None,
                    document_payload=None,
                )
            )
            continue

        def agreed(candidates: Sequence[JQuantsFinancialSummary], field: str) -> float | None:
            values = {getattr(row, field) for row in candidates if getattr(row, field) is not None}
            return (
                next(iter(values))
                if len(values) == 1
                and not any(field in row.unresolved_actual_fields for row in candidates)
                else None
            )

        eps = (
            agreed(tied, "cumulative_eps")
            if len({statement_basis(row) for row in tied}) == 1
            else None
        )
        dps = agreed(tied, "dps_actual_annual")
        # EPS and payout are independent facts. The capital pair remains exactly
        # the representative source's pair; never fill its missing half from another.
        result.append(
            replace(
                representative,
                cumulative_eps=eps,
                dps_actual_annual=dps,
                disclosure_number=None,
                document_payload=None,
            )
        )
    return result


def _asof_basis_fy_dps(
    fy_rows: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    asof_date: date,
) -> dict[date, float]:
    """通期実績 DPS を fiscal year end で引けるようにする。

    実績 DPS は資本の正規化でも原本値のまま残す。
    会計期間から比較日までに株数単位の変更・未分類 action があれば正の配当は未解決にする。
    その年度を未解決とし、未解決年度を含む YoY と streak は
    `_latest_consecutive_values` が None を返して増配と減配を取り違えない。
    """
    values: dict[date, float] = {}
    for row in fy_rows:
        fiscal_year_end = row.fiscal_year_end
        value = row.dps_actual_annual
        if fiscal_year_end is None or value is None or value < 0:
            continue
        if value == 0:
            values[fiscal_year_end] = value
            continue
        if period_days(row) is None:
            continue
        assert row.period_start is not None
        if not _dividend_basis_requires_resolution(
            ticker_bars, accrual_start=row.period_start, basis_date=asof_date
        ):
            values[fiscal_year_end] = value
    return values


def _latest_consecutive_values(
    fy_rows: Sequence[JQuantsFinancialSummary],
    values: Mapping[date, float],
    *,
    count: int,
    annual: bool,
) -> tuple[float, ...] | None:
    """実期間が隣接する直近FY。年間flowと期末株数の適格性は分ける。"""
    if len(fy_rows) < count:
        return None
    selected = fy_rows[-count:]
    resolved: list[float] = []
    for row in selected:
        if (
            row.fiscal_year_end is None
            or row.fiscal_year_end not in values
            or row.period_end != row.fiscal_year_end
            or period_days(row) is None
        ):
            return None
        assert row.period_start is not None
        assert row.period_end is not None
        if annual and not is_annual_period(row.period_start, row.period_end):
            return None
        resolved.append(values[row.fiscal_year_end])
    for prior, current in pairwise(selected):
        assert prior.period_end is not None
        if current.period_start != prior.period_end + timedelta(days=1):
            return None
    return tuple(resolved)


def _latest_row_with_value(
    fy_rows: Sequence[JQuantsFinancialSummary], values: Mapping[date, float]
) -> JQuantsFinancialSummary | None:
    if not fy_rows:
        return None
    row = fy_rows[-1]
    return row if row.fiscal_year_end is not None and row.fiscal_year_end in values else None


def _dividend_initiation(
    *,
    latest_pair: tuple[float, ...] | None,
    latest_actual: float | None,
    forecast: float | None,
) -> bool | None:
    if latest_pair is not None:
        prior, latest = latest_pair
        if prior == 0 and latest > 0:
            return True
        if latest > 0:
            return False
    if latest_actual == 0 and forecast is not None:
        return forecast > 0
    return None


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
    assets_row = _latest_actual_row(available, field_names=("total_assets",))
    operating_rows = _ttm_components(available, "operating_profit", ttm_rules)
    sales_rows = _ttm_components(available, "sales", ttm_rules)

    def ratio(numerator: float | None, denominator: float | None) -> float | None:
        if numerator is None or denominator is None or denominator <= 0:
            return None
        return numerator / denominator

    return ProfitabilityLevelSignals(
        operating_profit_to_assets=(
            ratio(operating_profit_ttm, total_assets)
            if assets_row is not None and comparable_reporting_basis((*operating_rows, assets_row))
            else None
        ),
        operating_margin=(
            ratio(operating_profit_ttm, sales_ttm)
            if operating_rows
            and sales_rows
            and comparable_reporting_basis((*operating_rows, *sales_rows))
            else None
        ),
        asset_turnover=(
            ratio(sales_ttm, total_assets)
            if assets_row is not None and comparable_reporting_basis((*sales_rows, assets_row))
            else None
        ),
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
    latest_basis = (
        (statement_basis(fy_rows[-1]), accounting_standard(fy_rows[-1])) if fy_rows else None
    )
    eps_values = {
        row.fiscal_year_end: row.cumulative_eps
        for row in fy_rows
        if row.fiscal_year_end is not None
        and row.cumulative_eps is not None
        and row.period_end == row.fiscal_year_end
        and (days := period_days(row)) is not None
        and ttm_rules.full_year_min_days <= days <= ttm_rules.full_year_max_days
        and (statement_basis(row), accounting_standard(row)) == latest_basis
        and accounting_standard(row) is not None
        and latest_reporting_basis_matches(
            [
                candidate
                for candidate in normalized
                if candidate.fiscal_year_end == row.fiscal_year_end
            ],
            row,
        )
    }
    latest_three = _latest_consecutive_values(fy_rows, eps_values, count=3, annual=True)
    latest_five = _latest_consecutive_values(fy_rows, eps_values, count=5, annual=True)

    def normalized_per(values: tuple[float, ...] | None) -> float | None:
        if values is None or close is None or close <= 0:
            return None
        average = fmean(values)
        return close / average if isfinite(average) and average > 0 else None

    return NormalizedProfitSignals(
        normalized_per_3fy=normalized_per(latest_three),
        normalized_per_5fy=normalized_per(latest_five),
    )
