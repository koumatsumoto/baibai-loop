"""Offline JPX TOPIX gross-total-return observations for portfolio outcomes."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from baibai_loop.foundation.benchmark_observation import benchmark_observation_error
from baibai_loop.foundation.yaml_io import safe_load

_CONFIG = ConfigDict(frozen=True, strict=True, extra="forbid")


class BenchmarkObservationError(ValueError):
    """Raised when a JPX benchmark observation is incomplete or malformed."""


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
    cumulative_return_pct: float
    annualized_return_pct: float | None
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
        error = benchmark_observation_error(
            period_start_date=self.period_start_date,
            period_end_date=self.period_end_date,
            source_as_of=self.source_as_of,
            published_at=self.published_at,
            retrieved_at=self.retrieved_at,
            horizon=self.horizon,
            period_basis=self.period_basis,
            period_rule_source_url=self.period_rule_source_url,
            cumulative_return_pct=self.cumulative_return_pct,
            annualized_return_pct=self.annualized_return_pct,
            display_precision_bps=self.display_precision_bps,
        )
        if error is not None:
            raise ValueError(error)
        return self


def load_benchmark_observation(path: Path) -> BenchmarkObservation:
    try:
        raw = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise BenchmarkObservationError(f"failed to read benchmark observation: {error}") from error
    if not isinstance(raw, dict):
        raise BenchmarkObservationError("benchmark observation root must be a mapping")
    try:
        return BenchmarkObservation.model_validate(raw)
    except ValidationError as error:
        raise BenchmarkObservationError(str(error)) from error


def benchmark_observation_json_schema() -> dict[str, object]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "benchmark-observation",
        **BenchmarkObservation.model_json_schema(),
    }
