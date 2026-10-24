"""両分析で意味が共通する実績の矛盾処理・会計期間比較・金額算術。I/Oなし。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from baibai_engine.foundation.date_utils import add_months_clamped
from baibai_engine.market.financial_documents import (
    AccountingPeriodKey,
    accounting_period_identity,
    latest_disclosure_candidates,
)
from baibai_engine.market.jquants_models import JQuantsFinancialSummary

ACTUAL_FIELDS = (
    "sales",
    "cfo",
    "cash_eq",
    "total_assets",
    "equity",
    "operating_profit",
    "ordinary_profit",
    "profit",
    "eps_ttm",
    "bps",
    "shares_outstanding",
    "treasury_shares",
    "equity_to_asset_ratio",
    "dps_actual_annual",
    "average_shares",
)


def resolve_actual_conflicts(
    summaries: Sequence[JQuantsFinancialSummary],
) -> list[JQuantsFinancialSummary]:
    """Mask only contradictory fields in a period/revision cohort; keep source rows."""
    groups: dict[AccountingPeriodKey, list[JQuantsFinancialSummary]] = {}
    for row in summaries:
        groups.setdefault(accounting_period_identity(row), []).append(row)
    changes: dict[int, JQuantsFinancialSummary] = {}
    for group in groups.values():
        for field in ACTUAL_FIELDS:
            observed = [
                row
                for row in group
                if getattr(row, field) is not None or field in row.unresolved_actual_fields
            ]
            latest = latest_disclosure_candidates(observed)
            if len({getattr(row, field) for row in latest if getattr(row, field) is not None}) <= 1:
                continue
            for row in latest:
                current = changes.get(id(row), row)
                values: dict[str, Any] = {
                    field: None,
                    "unresolved_actual_fields": tuple(
                        sorted(set(current.unresolved_actual_fields) | {field})
                    ),
                }
                changes[id(row)] = replace(current, **values)
    return [changes.get(id(row), row) for row in summaries]


def period_days(summary: JQuantsFinancialSummary) -> int | None:
    if (
        summary.period_start is None
        or summary.period_end is None
        or summary.period_end > summary.disclosed_at
    ):
        return None
    days = (summary.period_end - summary.period_start).days + 1
    return days if days > 0 else None


def prior_year_period_matches(
    prior: JQuantsFinancialSummary,
    current: JQuantsFinancialSummary,
    *,
    length_tolerance_ratio: float,
    end_tolerance_days: int,
) -> bool:
    """Compare actual dates and lengths, including 52/53-week reporting calendars."""
    prior_days, current_days = period_days(prior), period_days(current)
    if prior_days is None or current_days is None:
        return False
    assert prior.period_start is not None
    assert prior.period_end is not None
    assert current.period_start is not None
    assert current.period_end is not None
    return (
        abs(prior_days - current_days) <= max(1, round(current_days * length_tolerance_ratio))
        and abs((prior.period_start - add_months_clamped(current.period_start, -12)).days)
        <= end_tolerance_days
        and abs((prior.period_end - add_months_clamped(current.period_end, -12)).days)
        <= end_tolerance_days
    )


def compose_ttm_amount(current: float, prior_year: float, prior_same: float) -> float:
    """Inputs are compatible amounts, never reported EPS or share counts."""
    return current + prior_year - prior_same
