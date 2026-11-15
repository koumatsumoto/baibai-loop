"""個別評価・資本配分・保有判断へ同じ権利単位の価値換算を産む純粋算術。

換算は条件付き、税費用控除前、分配再投資なし。期待値や IRR ではない。
企業調査の根拠に基づく仮定は Stock Assessment で評価し、Independent Review で検証する。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from baibai_engine.primitives.numbers import finite_decimal

FIXED_VALUE_DELAY_MONTHS = 12


class Projection(BaseModel):
    """起点の未調整価格一株に対応する分配後の残余価値と期間内累積分配。"""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid", allow_inf_nan=False)
    terminal_value_per_share_yen: Annotated[Decimal, Field(ge=0)]
    cash_distribution_per_share_yen: Annotated[Decimal, Field(ge=0)]
    calculation: Annotated[str, Field(min_length=1, pattern=r"\S")]
    source_ids: Annotated[tuple[str, ...], Field(min_length=1)]

    @field_validator(
        "terminal_value_per_share_yen", "cash_distribution_per_share_yen", mode="before"
    )
    @classmethod
    def _number(cls, value: object) -> Decimal:
        return finite_decimal(value)

    @field_validator("source_ids", mode="before")
    @classmethod
    def _sources(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


def _horizon(months: object) -> Decimal:
    if isinstance(months, bool) or not isinstance(months, int) or months <= 0:
        raise ValueError("horizon_months must be a positive integer")
    return Decimal(months) / 12


def _positive(value: object, name: str) -> Decimal:
    number = finite_decimal(value)
    if number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


@dataclass(frozen=True, slots=True)
class ReturnProjection:
    total_value_yen: Decimal
    total_return_pct: Decimal
    annualized_return_pct: Decimal


def project_return(
    projection: Projection, *, price_yen: Decimal, horizon_months: int
) -> ReturnProjection:
    """表示丸めをBaseとDownsideの年率比較へ持ち込まない。"""
    with localcontext() as context:
        context.prec = 50
        price = _positive(price_yen, "price_yen")
        horizon = _horizon(horizon_months)
        wealth = (
            projection.terminal_value_per_share_yen + projection.cash_distribution_per_share_yen
        )
        ratio = wealth / price
        return ReturnProjection(
            wealth,
            (ratio - 1) * 100,
            (ratio ** (1 / horizon) - 1) * 100 if wealth else Decimal(-100),
        )


def project_buy_capital(
    base: Projection,
    downside: Projection,
    *,
    horizon_months: int,
    limit: Decimal,
    cash: Decimal,
    quantity: int,
    author_selected: bool,
) -> dict[str, object]:
    """同じ未拘束cashの全量指値成立例。既保有/予約の価値や約定確率は含めない。"""
    with localcontext() as arithmetic:
        arithmetic.prec = 50
        notional = limit * quantity
        unspent = cash - notional
        result: dict[str, object] = {
            "scope": "available_cash_only",
            "execution_assumption": "full_fill_at_limit",
            "return_basis": "conditional_pretax_without_reinvestment",
            "investor_taxes_and_fees_included": False,
            "cash_basis": "constant_nominal_without_interest",
            "horizon_months": horizon_months,
            "quantity_basis": "author_selected" if author_selected else "quantity_ceiling",
            "quantity": quantity,
            "limit_price_yen": str(limit),
            "notional_yen": str(notional),
            "unspent_cash_yen": str(unspent),
            "cash_only_terminal_value_yen": str(cash),
        }
        for name, projection in (("base", base), ("downside", downside)):
            total = (
                project_return(
                    projection, price_yen=limit, horizon_months=horizon_months
                ).total_value_yen
                * quantity
            )
            result[name] = {
                "investment_total_value_yen": str(total),
                "profit_loss_yen": str(total - notional),
                "capital_total_value_yen": str(unspent + total),
            }
        return result


def maximum_entry_price(
    projection: Projection, *, horizon_months: int, required_annual_return_pct: Decimal
) -> Decimal:
    """Return raw Pmax; broker tick rounding is outside valuation."""
    with localcontext() as context:
        context.prec = 50
        horizon = _horizon(horizon_months)
        rate = _positive(required_annual_return_pct, "required_annual_return_pct") / 100
        wealth = (
            projection.terminal_value_per_share_yen + projection.cash_distribution_per_share_yen
        )
        return wealth / (1 + rate) ** horizon


def required_total_value(
    *, price_yen: Decimal, horizon_months: int, required_annual_return_pct: Decimal
) -> Decimal:
    """Value needed at a price, using the requirement recorded in the Stock Assessment."""
    with localcontext() as context:
        context.prec = 50
        price = _positive(price_yen, "price_yen")
        horizon = _horizon(horizon_months)
        rate = _positive(required_annual_return_pct, "required_annual_return_pct") / 100
        return price * (1 + rate) ** horizon


@dataclass(frozen=True, slots=True)
class ValuationConditions:
    required_total_value_yen: Decimal
    required_terminal_value_per_share_yen: Decimal
    total_value_surplus_yen: Decimal
    returns: ReturnProjection
    delayed_returns: ReturnProjection


def valuation_conditions(
    projection: Projection,
    *,
    price_yen: Decimal,
    horizon_months: int,
    required_annual_return_pct: Decimal,
) -> ValuationConditions:
    """Recorded price requirements and h+12 sensitivity with terminal/cash held fixed."""
    with localcontext() as context:
        context.prec = 50
        required = required_total_value(
            price_yen=price_yen,
            horizon_months=horizon_months,
            required_annual_return_pct=required_annual_return_pct,
        )
        returns = project_return(projection, price_yen=price_yen, horizon_months=horizon_months)
        # 価値固定の時間感応度であり、遅延による利益・資金の悪化は仮定しない。
        delayed = project_return(
            projection,
            price_yen=price_yen,
            horizon_months=horizon_months + FIXED_VALUE_DELAY_MONTHS,
        )
        return ValuationConditions(
            required,
            max(Decimal(0), required - projection.cash_distribution_per_share_yen),
            returns.total_value_yen - required,
            returns,
            delayed,
        )
