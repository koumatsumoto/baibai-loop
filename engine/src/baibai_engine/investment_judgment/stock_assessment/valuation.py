"""銘柄評価に含まれる企業固有の評価前提。L2 の企業価値モデルとは区別する。"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from baibai_engine.investment_judgment.stock_assessment.scenario import Projection
from baibai_engine.primitives.numbers import finite_decimal

_CONFIG = ConfigDict(frozen=True, strict=True, extra="forbid", allow_inf_nan=False)


class Valuation(BaseModel):
    model_config = _CONFIG
    status: Literal["resolved", "unresolved"]
    market_price_fact_id: str | None
    horizon_months: Annotated[int, Field(gt=0)] | None
    required_annual_return_pct: Annotated[Decimal, Field(gt=0)] | None
    base: Projection | None
    downside: Projection | None
    unresolved_reason: str | None

    @field_validator("required_annual_return_pct", mode="before")
    @classmethod
    def _rate(cls, value: object) -> Decimal | None:
        return None if value is None else finite_decimal(value)

    @model_validator(mode="after")
    def _resolution(self) -> Valuation:
        values = (self.horizon_months, self.required_annual_return_pct, self.base, self.downside)
        if self.status == "resolved":
            if any(value is None for value in values) or not self.market_price_fact_id:
                raise ValueError("resolved valuation requires price, horizon, rate and projections")
            if self.unresolved_reason is not None:
                raise ValueError("resolved valuation forbids unresolved_reason")
        elif (
            any(value is not None for value in values) or not (self.unresolved_reason or "").strip()
        ):
            raise ValueError(
                "unresolved valuation requires a reason and null projections/horizon/rate"
            )
        return self
