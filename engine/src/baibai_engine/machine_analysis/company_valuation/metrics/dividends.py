"""配当・自己株取得と株主還元を、予想と実績のbasisで計算する。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from itertools import pairwise

from baibai_engine.foundation.date_utils import add_months_clamped
from baibai_engine.market.financial_documents import (
    accounting_period_identity,
    latest_disclosure_candidates,
    latest_forecast_source,
    project_financial_documents,
    statement_basis,
)
from baibai_engine.market.financial_periods import period_days, resolve_actual_conflicts
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.market.price_basis import cumulative_share_unit_factor_after, share_unit_factor
from baibai_engine.market.price_models import JQuantsAdjustmentFactorEvent, JQuantsDailyBar
from baibai_engine.machine_analysis.company_valuation.metrics.capital import _normalize_summaries_to_asof_basis
from baibai_engine.machine_analysis.company_valuation.metrics.models import (
    UNRESOLVED_ACTUAL_DIVIDEND_BASIS,
    UNRESOLVED_DIVIDEND_BASIS,
)
from baibai_engine.machine_analysis.company_valuation.metrics.periods import _accounting_observation_key

# 5 年 carry へ反復させる会社予想の上限。直近実績の 2 倍を超える進行期予想は、
# 特別配当か未実現の還元転換かを機械入力から区別できない。実績が正なら既存の実績経路へ
# 倒し、実績 0 / 未観測は初配当を消さないため予想を使う。
_DIVIDEND_FORECAST_SPIKE_MULTIPLE = 2.0


@dataclass(frozen=True, slots=True)
class ShareholderReturnChangeSignals:
    """Disclosure-date-filtered facts used only by the calibration panel."""

    dps_streak_up: bool | None
    dps_yoy_latest: float | None
    dps_guidance_up: bool | None
    dividend_initiation: bool | None
    share_count_reduction_streak: int | None
    shareholder_return_change: bool | None


@dataclass(frozen=True, slots=True, kw_only=True)
class _DividendCarry:
    """carry 用に解決した配当利回りと、その基準・分割 factor の事実記録。"""

    dividend_yield: float | None
    dps_actual_annual: float | None
    dps_forecast_annual: float | None
    basis: str
    split_factor: float | None


def _forecast_annual_dps(
    summaries: Sequence[JQuantsFinancialSummary],
) -> float | None:
    """Return guidance after the caller resolved its applicable period and basis."""
    source = latest_forecast_source(summaries, dividend=True)
    return source.dps_forecast_annual if source is not None else None


def _actual_dps_rows(
    summaries: Sequence[JQuantsFinancialSummary],
) -> list[JQuantsFinancialSummary]:
    return sorted(
        [
            row
            for row in resolve_actual_conflicts(summaries)
            if row.dps_actual_annual is not None
            or "dps_actual_annual" in row.unresolved_actual_fields
        ],
        key=_accounting_observation_key,
        reverse=True,
    )


def _dividend_basis_requires_resolution(
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    *,
    accrual_start: date,
    basis_date: date,
) -> bool:
    """Whether annual DPS spans a unit change or an unclassified action.

    Opposing splits still require payment-date resolution even if their product
    is one. A rights issue alone does not change the unit of cash paid per share.
    """
    return any(
        accrual_start < bar.traded_at <= basis_date and share_unit_factor(bar) != 1.0
        for bar in ticker_bars
    )


def _resolve_dividend_carry(
    summaries: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    latest_price: float,
    asof_date: date,
) -> _DividendCarry:
    """Resolve carry from applicable-period guidance and split-safe actual payouts.

    Issuers may report payment DPS on different share bases. When a unit change
    falls inside the accrual-to-comparison-date window, the source does not establish
    a convertible basis, even when payment details add up or split factors cancel.
    An explicit zero is basis-independent; raw payment facts remain in L1.

    Prefer nonnegative guidance from the document-period resolver.
    When positive actual DPS exists,
    guidance over twice that amount falls back to actual for recurring carry.
    Zero or unavailable actual DPS does not impose that spike ceiling.
    """
    actual_rows = _actual_dps_rows(summaries)
    forecast = _forecast_annual_dps(summaries)
    basis_factor: float | None = None
    requires_resolution = False
    actual_annual: float | None = None
    actual_unresolved = bool(
        actual_rows
        and (
            "dps_actual_annual" in actual_rows[0].unresolved_actual_fields
            or (actual_rows[0].dps_actual_annual != 0 and period_days(actual_rows[0]) is None)
        )
    )
    if actual_rows and not actual_unresolved:
        latest_actual = actual_rows[0]
        assert latest_actual.dps_actual_annual is not None
        accrual_start = latest_actual.period_start
        requires_resolution = accrual_start is not None and _dividend_basis_requires_resolution(
            ticker_bars, accrual_start=accrual_start, basis_date=asof_date
        )
        if requires_resolution:
            assert accrual_start is not None
            basis_factor = cumulative_share_unit_factor_after(ticker_bars, accrual_start, asof_date)
            actual_annual = 0.0 if latest_actual.dps_actual_annual == 0 else None
        else:
            actual_annual = latest_actual.dps_actual_annual

    recorded_factor = basis_factor

    forecast_is_usable_for_carry = (
        forecast is not None
        and forecast >= 0
        and (
            actual_annual is None
            or actual_annual <= 0
            or forecast <= _DIVIDEND_FORECAST_SPIKE_MULTIPLE * actual_annual
        )
    )

    if latest_price <= 0:
        basis = "unavailable"
    elif forecast_is_usable_for_carry and forecast is not None:
        return _DividendCarry(
            dividend_yield=forecast / latest_price,
            dps_actual_annual=actual_annual,
            dps_forecast_annual=forecast,
            basis="forecast_annual",
            split_factor=recorded_factor,
        )
    elif actual_annual is not None and actual_annual >= 0:
        return _DividendCarry(
            dividend_yield=actual_annual / latest_price,
            dps_actual_annual=actual_annual,
            dps_forecast_annual=forecast,
            basis="actual_reported",
            split_factor=recorded_factor,
        )
    elif actual_unresolved:
        basis = UNRESOLVED_ACTUAL_DIVIDEND_BASIS
    elif requires_resolution and actual_annual is None:
        # 正の実績が解決できなかった年度は無配と区別する。
        basis = UNRESOLVED_DIVIDEND_BASIS
    else:
        basis = "unavailable"

    return _DividendCarry(
        dividend_yield=None,
        dps_actual_annual=actual_annual,
        dps_forecast_annual=forecast,
        basis=basis,
        split_factor=recorded_factor,
    )


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
    latest_pair = _latest_consecutive_values(fy_rows, dps_values, count=2)
    latest_three = _latest_consecutive_values(fy_rows, dps_values, count=3)

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
    latest_share_three = _latest_consecutive_values(fy_rows, share_values, count=3)
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
                row.eps_ttm is not None,
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
                    eps_ttm=None,
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

        eps = agreed(tied, "eps_ttm") if len({statement_basis(row) for row in tied}) == 1 else None
        dps = agreed(tied, "dps_actual_annual")
        # EPS and payout are independent facts. The capital pair remains exactly
        # the representative source's pair; never fill its missing half from another.
        result.append(
            replace(
                representative,
                eps_ttm=eps,
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
) -> tuple[float, ...] | None:
    if len(fy_rows) < count:
        return None
    selected = list(fy_rows[-count:])
    for prior, current in pairwise(selected):
        if current.fiscal_year_end is None or prior.fiscal_year_end is None:
            return None
        if add_months_clamped(current.fiscal_year_end, -12) != prior.fiscal_year_end:
            return None
    resolved: list[float] = []
    for row in selected:
        if row.fiscal_year_end is None or row.fiscal_year_end not in values:
            return None
        resolved.append(values[row.fiscal_year_end])
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
