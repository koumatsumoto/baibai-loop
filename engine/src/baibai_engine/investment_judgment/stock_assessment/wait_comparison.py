"""同じ元手・終期で、仮の指値が成立する場合としない場合を別に計算する。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from baibai_engine.investment_judgment.contracts import _date
from baibai_engine.investment_judgment.stock_assessment.scenario import (
    Projection,
    required_total_value,
)
from baibai_engine.primitives.dates import add_months_clamped
from baibai_engine.primitives.numbers import finite_decimal


class WaitingTerms(BaseModel):
    """作者が指定する研究上の期限、資金、単元と費用・権利。実口座ではない。"""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid", allow_inf_nan=False)
    initial_cash_yen: Annotated[Decimal, Field(gt=0)]
    deadline: date
    lot_size: Annotated[int, Field(gt=0)]
    partial_fill_quantity: Annotated[int, Field(gt=0)] | None = None
    entry_fee_bps: Annotated[Decimal, Field(ge=0, lt=10000)]
    terminal_sale_fee_bps: Annotated[Decimal, Field(ge=0, lt=10000)]
    base_missed_distribution_per_share_yen: Annotated[Decimal, Field(ge=0)]
    downside_missed_distribution_per_share_yen: Annotated[Decimal, Field(ge=0)]

    @field_validator("deadline", mode="before")
    @classmethod
    def _deadline(cls, value: object) -> date:
        return _date(value)

    @field_validator(
        "initial_cash_yen",
        "entry_fee_bps",
        "terminal_sale_fee_bps",
        "base_missed_distribution_per_share_yen",
        "downside_missed_distribution_per_share_yen",
        mode="before",
    )
    @classmethod
    def _decimal(cls, value: object) -> Decimal:
        return finite_decimal(value)


def waiting_paths(
    terms: WaitingTerms,
    *,
    reference_price: Decimal,
    reference_price_as_of: date,
    limit: Decimal,
    quantity: int | None,
    as_of: date,
    horizon_months: int,
    required_annual_return_pct: Decimal,
    base: Projection,
    downside: Projection,
) -> dict[str, object]:
    end_date = add_months_clamped(as_of, horizon_months)
    if not as_of < terms.deadline <= end_date:
        raise ValueError("wait deadline must follow the valuation date and be within the horizon")
    if limit >= reference_price:
        raise ValueError("waiting comparison requires a price below the reference")
    if quantity is None or quantity <= 0 or quantity % terms.lot_size:
        raise ValueError("waiting comparison requires a positive whole-lot quantity")
    partial = terms.partial_fill_quantity
    if partial is not None and (partial >= quantity or partial % terms.lot_size):
        raise ValueError("partial fill must be a smaller positive whole-lot quantity")
    with localcontext() as context:
        context.prec = 50
        cash = terms.initial_cash_yen
        entry_factor = 1 + terms.entry_fee_bps / 10000
        exit_factor = 1 - terms.terminal_sale_fee_bps / 10000
        now_outlay = quantity * reference_price * entry_factor
        limit_outlay = quantity * limit * entry_factor
        if limit_outlay > cash:
            raise ValueError("initial cash cannot cover the intended limit and its fee")
        now_cash = cash - now_outlay if now_outlay <= cash else None
        fills = [0, quantity] if partial is None else [0, partial, quantity]
        cases = {}
        for name, projection, missed in (
            ("base", base, terms.base_missed_distribution_per_share_yen),
            ("downside", downside, terms.downside_missed_distribution_per_share_yen),
        ):
            dist = projection.cash_distribution_per_share_yen
            if missed > dist:
                raise ValueError(f"{name} foregone distribution exceeds its total projection")
            net_sale = projection.terminal_value_per_share_yen * exit_factor
            now_wealth = None if now_cash is None else now_cash + quantity * (net_sale + dist)
            paths = []
            for count in fills:
                unspent = cash - count * limit * entry_factor
                wealth = unspent + count * (net_sale + dist - missed)
                paths.append(
                    {
                        "filled_quantity": count,
                        "cash_after_deadline_yen": str(unspent),
                        "terminal_wealth_yen": str(wealth),
                        "difference_vs_buy_now_yen": None
                        if now_wealth is None
                        else str(wealth - now_wealth),
                        "difference_vs_cash_yen": str(wealth - cash),
                    }
                )
            cases[name] = {
                "buy_now_terminal_wealth_yen": None if now_wealth is None else str(now_wealth),
                "wait_paths": paths,
            }
        return {
            "scope": "hypothetical_cash_only_no_orders_or_fill_probabilities",
            "wealth_basis": "conditional_terminal_sale_plus_eligible_distributions",
            "cash_basis": "constant_nominal_without_interest_or_reinvestment",
            "taxes_spread_and_market_impact_included": False,
            "assumptions": terms.model_dump(mode="json"),
            "valuation_as_of": as_of.isoformat(),
            "terminal_date": end_date.isoformat(),
            "reference_price_yen": str(reference_price),
            "reference_price_as_of": reference_price_as_of.isoformat(),
            "comparison_limit_yen": str(limit),
            "intended_quantity": quantity,
            "cash_only_terminal_wealth_yen": str(cash),
            "required_annual_return_pct": str(required_annual_return_pct),
            "whole_cash_required_terminal_wealth_yen": str(
                required_total_value(
                    price_yen=cash,
                    horizon_months=horizon_months,
                    required_annual_return_pct=required_annual_return_pct,
                )
            ),
            "buy_now_cash_status": "sufficient" if now_cash is not None else "insufficient",
            "buy_now_unspent_cash_yen": None if now_cash is None else str(now_cash),
            "cash_outside_full_limit_set_aside_yen": str(cash - limit_outlay),
            "cases": cases,
        }
