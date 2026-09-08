"""個別評価・資本配分・保有判断へ同じ権利単位の価値換算を産む純粋算術。

換算は条件付き、税費用控除前、分配再投資なし。期待値や IRR ではない。
企業財務の推定と分配後の残余価値の妥当性は Research と独立 Review が確認する。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


def finite_decimal(value: object) -> Decimal:
    """Reject booleans and nonfinite numbers before arithmetic or comparison."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("must be a finite decimal number")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("must be a finite decimal number") from error
    if not number.is_finite():
        raise ValueError("must be a finite decimal number")
    return number


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
    """Use identical unrounded arithmetic for Base and Downside, including total loss."""
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
    """Value needed at a price, using the requirement recorded in the Thesis."""
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
        delayed = project_return(
            projection, price_yen=price_yen, horizon_months=horizon_months + 12
        )
        return ValuationConditions(
            required,
            max(Decimal(0), required - projection.cash_distribution_per_share_yen),
            returns.total_value_yen - required,
            returns,
            delayed,
        )
