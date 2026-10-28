"""符号付きの実績利益・価格観測。期待収益率や購入判断ではない。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from math import isfinite

from baibai_engine.market.financial_documents import (
    accounting_period_identity,
    latest_disclosure_candidates,
    project_financial_documents,
    statement_basis,
)
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.market.price_models import (
    JQuantsAdjustmentFactorEvent,
    JQuantsDailyBar,
    PriceBar,
)

from baibai_engine.machine_analysis.models.company_financials.metrics.capital import _normalize_summaries_with_status, _resolve_capital_basis
from baibai_engine.machine_analysis.models.company_financials.metrics.models import TTMRules
from baibai_engine.machine_analysis.models.company_financials.metrics.periods import _actual_rows, _ttm_components


@dataclass(frozen=True, slots=True)
class ProfitComponent:
    coefficient: int
    amount_yen: float
    period_start: date | None
    period_end: date | None
    disclosed_at: date
    disclosed_time: str | None
    disclosure_number: str | None
    document_type: str | None


@dataclass(frozen=True, slots=True)
class EarningsPrice:
    as_of: date
    price_date: date | None
    price_yen: float | None
    shares_ex_treasury: float | None
    market_cap_yen: float | None
    profit_ttm_yen: float | None
    statement_basis: str | None
    profit_components: tuple[ProfitComponent, ...]
    earnings_yield: float | None
    unavailable_reason: str | None


def earnings_price(
    summaries: Sequence[JQuantsFinancialSummary],
    *,
    adjusted_price: PriceBar | None,
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    as_of: date,
    ttm_rules: TTMRules,
) -> EarningsPrice:
    """同一基準へ揃えた株数と価格で時価総額を作り、年間利益を一度だけ割る。

    adjusted_priceはas_of基準へ調整済みの価格。開示EPSの足し算やPERの逆数は使わない。
    連結/単体が不明なら原額は保持するが選定用利回りを確定しない。
    """
    projected = sorted(
        project_financial_documents(summaries, as_of),
        key=lambda row: (row.disclosed_at, row.disclosed_time or "", row.disclosure_number or ""),
    )
    normalized = _normalize_summaries_with_status(projected, adjustment_events, as_of)
    capital = _resolve_capital_basis(
        normalized.summaries, capital_basis_barrier=normalized.capital_basis_barrier
    )
    rows = _ttm_components(normalized.summaries, "profit", ttm_rules)
    components = tuple(
        ProfitComponent(
            coefficient=-1 if index == 2 else 1,
            amount_yen=row.profit,
            period_start=row.period_start,
            period_end=row.period_end,
            disclosed_at=row.disclosed_at,
            disclosed_time=row.disclosed_time,
            disclosure_number=row.disclosure_number,
            document_type=row.document_type,
        )
        for index, row in enumerate(rows)
        if row.profit is not None
    )
    profit = sum(part.coefficient * part.amount_yen for part in components) if components else None
    basis = statement_basis(rows[0]) if rows else None
    basis_conflict = any(
        {
            statement_basis(candidate)
            for candidate in latest_disclosure_candidates(
                [
                    candidate
                    for candidate in _actual_rows(normalized.summaries)
                    if accounting_period_identity(candidate) == accounting_period_identity(row)
                ]
            )
        }
        != {statement_basis(row)}
        for row in rows
    )
    price = adjusted_price.price if adjusted_price is not None else None
    price_date = adjusted_price.traded_at if adjusted_price is not None else None
    shares = capital.shares_ex_treasury
    market_cap = None
    reason = None
    if price is None or price_date is None:
        reason = "missing_price"
    elif price_date > as_of or not isfinite(price) or price <= 0:
        reason = "invalid_price"
    elif shares is None or shares <= 0:
        reason = capital.failure_reason or "missing_capital_basis"
    else:
        market_cap = price * shares
        if not isfinite(market_cap) or market_cap <= 0:
            market_cap = None
            reason = "invalid_market_cap"
    if reason is None and profit is None:
        reason = "unavailable_annual_profit"
    if reason is None and basis_conflict:
        reason = "conflicting_statement_basis"
    if reason is None and basis not in ("consolidated", "nonconsolidated"):
        reason = "unknown_statement_basis"
    value = profit / market_cap if reason is None and profit is not None and market_cap else None
    if value is not None and not isfinite(value):
        value = None
        reason = "invalid_earnings_yield"
    return EarningsPrice(
        as_of=as_of,
        price_date=price_date,
        price_yen=price,
        shares_ex_treasury=shares,
        market_cap_yen=market_cap,
        profit_ttm_yen=profit,
        statement_basis=None if basis_conflict else basis,
        profit_components=components,
        earnings_yield=value,
        unavailable_reason=reason,
    )
