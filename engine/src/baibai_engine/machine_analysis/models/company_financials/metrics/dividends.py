"""配当利回りを、予想と実績のbasisを区別して計算する。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    UNRESOLVED_ACTUAL_DIVIDEND_BASIS,
    UNRESOLVED_DIVIDEND_BASIS,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.periods import (
    _accounting_observation_key,
)
from baibai_engine.machine_analysis.observations.financials.financial_documents import (
    latest_forecast_source,
)
from baibai_engine.machine_analysis.observations.financials.financial_periods import (
    period_days,
    resolve_actual_conflicts,
)
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.prices.price_basis import (
    cumulative_share_unit_factor_after,
    share_unit_factor,
)
from baibai_engine.machine_analysis.observations.prices.price_models import (
    JQuantsAdjustmentFactorEvent,
    JQuantsDailyBar,
)

# 5 年 carry へ反復させる会社予想の上限。直近実績の 2 倍を超える進行期予想は、
# 特別配当か未実現の還元転換かを機械入力から区別できない。実績が正なら既存の実績経路へ
# 倒し、実績 0 / 未観測は初配当を消さないため予想を使う。
_DIVIDEND_FORECAST_SPIKE_MULTIPLE = 2.0


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
    latest_price: float | None,
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

    if latest_price is None or latest_price <= 0:
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
