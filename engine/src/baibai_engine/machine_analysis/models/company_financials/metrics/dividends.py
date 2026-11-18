"""実績配当の報告期間・株式単位と出典を解決する。"""

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
    accounting_period_identity,
    latest_disclosure_candidates,
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


@dataclass(frozen=True, slots=True)
class _ReportedDividend:
    amount: float | None
    basis: str
    split_factor: float | None
    source: dict[str, object] | None


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


def _actual_dps_source(rows: Sequence[JQuantsFinancialSummary]) -> dict[str, object]:
    selected = rows[0]
    period = accounting_period_identity(selected)
    sources = latest_disclosure_candidates(
        [row for row in rows if accounting_period_identity(row) == period]
    )
    return {
        "period_type": selected.fiscal_period,
        "period_start": selected.period_start.isoformat() if selected.period_start else None,
        "period_end": selected.period_end.isoformat() if selected.period_end else None,
        "basis": "issuer_dividend",
        "documents": [
            {
                "disclosure_number": row.disclosure_number,
                "disclosed_at": row.disclosed_at.isoformat(),
                "disclosed_time": row.disclosed_time,
                "document_type": row.document_type,
            }
            for row in sorted(
                sources,
                key=lambda row: (row.disclosed_time or "", row.disclosure_number or ""),
            )
        ],
    }


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


def _reported_dividend(
    summaries: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    asof_date: date,
) -> _ReportedDividend:
    rows = _actual_dps_rows(summaries)
    if not rows:
        return _ReportedDividend(None, "unavailable", None, None)
    row = rows[0]
    source = _actual_dps_source(rows)
    amount = row.dps_actual_annual
    if "dps_actual_annual" in row.unresolved_actual_fields or (
        amount != 0 and period_days(row) is None
    ):
        return _ReportedDividend(None, UNRESOLVED_ACTUAL_DIVIDEND_BASIS, None, source)
    assert amount is not None
    start = row.period_start
    if start is not None and _dividend_basis_requires_resolution(
        ticker_bars, accrual_start=start, basis_date=asof_date
    ):
        # 記録した株式単位の変更を各支払額へ一律に掛けられない。明示ゼロだけは不変。
        factor = cumulative_share_unit_factor_after(ticker_bars, start, asof_date)
        if amount != 0:
            return _ReportedDividend(None, UNRESOLVED_DIVIDEND_BASIS, factor, source)
    else:
        factor = None
    return _ReportedDividend(
        amount,
        "actual_reported" if amount >= 0 else UNRESOLVED_ACTUAL_DIVIDEND_BASIS,
        factor,
        source,
    )
