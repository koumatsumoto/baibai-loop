"""公式TOPIX gross-total-returnの観測・期間・出所の純粋契約。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from baibai_engine.foundation.time import JST

_CONFIG = ConfigDict(frozen=True, strict=True, extra="forbid")


def _date(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("must be an ISO date")
    return date.fromisoformat(value)


def _datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("must be an ISO datetime")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("must include a timezone")
    return parsed


class BenchmarkObservation(BaseModel):
    """One official JPX period-return observation, not an inferred price proxy."""

    model_config = _CONFIG

    schema_version: Literal[1]
    kind: Literal["benchmark_observation"]
    benchmark_id: Literal["jpx-topix-gross-total-return"]
    index_name: Literal["TOPIX"]
    dividend_treatment: Literal["gross_total_return"]
    horizon: Literal["1y", "3y", "5y"]
    period_start_date: date
    period_end_date: date
    period_basis: Literal["official_explicit", "official_month_end_rule"]
    period_rule_source_url: Annotated[str, Field(pattern=r"^https://")] | None
    cumulative_return_pct: Annotated[float, Field(ge=-100, allow_inf_nan=False)]
    annualized_return_pct: Annotated[float, Field(ge=-100, allow_inf_nan=False)] | None
    display_precision_bps: Annotated[int, Field(ge=0, le=100)]
    source_url: Annotated[str, Field(pattern=r"^https://")]
    source_as_of: date
    published_at: date
    retrieved_at: datetime

    @field_validator(
        "period_start_date", "period_end_date", "source_as_of", "published_at", mode="before"
    )
    @classmethod
    def _parse_dates(cls, value: object) -> date:
        return _date(value)

    @field_validator("retrieved_at", mode="before")
    @classmethod
    def _parse_retrieved_at(cls, value: object) -> datetime:
        return _datetime(value)

    @model_validator(mode="after")
    def _validate_period(self) -> BenchmarkObservation:
        if self.period_start_date >= self.period_end_date:
            raise ValueError("period_start_date must precede period_end_date")
        if self.period_end_date != self.source_as_of:
            raise ValueError("period_end_date must equal source_as_of")
        years = int(self.horizon[:-1])
        if (
            self.period_end_date.year - self.period_start_date.year != years
            or self.period_start_date.month != self.period_end_date.month
            or abs(self.period_start_date.day - self.period_end_date.day) > 3
        ):
            raise ValueError("benchmark period does not match horizon")
        if self.period_basis == "official_month_end_rule" and self.period_rule_source_url is None:
            raise ValueError("official_month_end_rule requires period_rule_source_url")
        if self.period_basis == "official_explicit" and self.period_rule_source_url is not None:
            raise ValueError("official_explicit forbids period_rule_source_url")
        if (self.horizon == "1y") != (self.annualized_return_pct is None):
            raise ValueError("1y annualized_return_pct must be null; 3y/5y require a value")
        if self.annualized_return_pct is not None:
            derived = ((1 + self.cumulative_return_pct / 100) ** (1 / years) - 1) * 100
            tolerance_pct = self.display_precision_bps / 100 + 0.01
            if abs(derived - self.annualized_return_pct) > tolerance_pct:
                raise ValueError("annualized_return_pct disagrees with cumulative_return_pct")
        if self.published_at > self.retrieved_at.astimezone(JST).date():
            raise ValueError("published_at cannot be after retrieved_at")
        return self
