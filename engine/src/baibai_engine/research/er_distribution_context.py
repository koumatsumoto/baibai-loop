"""Fail-closed schema contract for the E[r] context consumed by research."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from itertools import pairwise
from math import isclose
from typing import Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

HURDLE_ER_ANNUAL = 0.085


class _Stats(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    median: float
    q25: float
    q10: float
    trap_rate: float = Field(ge=0, le=1)
    n: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_quantiles(self) -> Self:
        if not self.q10 <= self.q25 <= self.median:
            raise ValueError("realized-return quantiles must be ordered")
        return self


class _Basis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    basis: str
    ticker_equal: _Stats
    cohort_equal: _Stats


class _Band(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    band_id: str
    quintile: int | None = Field(ge=1, le=5)
    lower_er_annual: float | None
    upper_er_annual: float | None
    median_predicted_er_annual: float
    cohort_count: int = Field(gt=0)
    median_n: int = Field(gt=0)
    bases: list[_Basis]

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        expected_id = f"q{self.quintile}" if self.quintile is not None else "er_gte_8_5pct"
        if self.band_id != expected_id:
            raise ValueError("band id and quintile disagree")
        if self.quintile is None and self.lower_er_annual != HURDLE_ER_ANNUAL:
            raise ValueError("hurdle band must start at 8.5%")
        if [item.basis for item in self.bases] != [
            "fy_actual_dividend_total_return",
            "price_return_only",
        ]:
            raise ValueError("realized-return bases are missing, duplicated, or reordered")
        return self


class _Horizon(BaseModel):
    model_config = ConfigDict(extra="forbid")

    horizon: str
    asof_start: date
    asof_end: date
    cohort_count: int = Field(gt=0)
    bands: list[_Band]

    @model_validator(mode="after")
    def validate_bands(self) -> Self:
        if self.asof_end < self.asof_start or [item.band_id for item in self.bands] != [
            "q1",
            "q2",
            "q3",
            "q4",
            "q5",
            "er_gte_8_5pct",
        ]:
            raise ValueError("invalid horizon range or band sequence")
        quintiles = self.bands[:5]
        cutoffs = [item.upper_er_annual for item in quintiles]
        if cutoffs[-1] is not None or any(value is None for value in cutoffs[:-1]):
            raise ValueError("only Q5 may have an open upper bound")
        numeric = [float(value) for value in cutoffs[:-1] if value is not None]
        if any(right <= left for left, right in pairwise(numeric)):
            raise ValueError("E[r] cutoffs must be strictly increasing")
        if quintiles[0].lower_er_annual is not None or any(
            item.lower_er_annual is None
            or not isclose(item.lower_er_annual, numeric[index - 1], abs_tol=1e-12)
            for index, item in enumerate(quintiles[1:], start=1)
        ):
            raise ValueError("adjacent quintile bounds must be continuous")
        hurdle = self.bands[-1]
        if hurdle.upper_er_annual is not None:
            raise ValueError("hurdle band must have an open upper bound")
        if any(item.cohort_count != self.cohort_count for item in self.bands):
            raise ValueError("band coverage must match the horizon cohort count")
        return self


class _CommonWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asof_start: date | None
    asof_end: date | None
    cohort_count: int = Field(ge=0)
    horizons: list[_Horizon]

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.cohort_count == 0:
            if self.asof_start is not None or self.asof_end is not None or self.horizons:
                raise ValueError("empty common window must not carry evidence")
            return self
        if (
            self.asof_start is None
            or self.asof_end is None
            or self.asof_end < self.asof_start
            or [item.horizon for item in self.horizons] != ["3y", "5y"]
            or any(item.cohort_count != self.cohort_count for item in self.horizons)
        ):
            raise ValueError("common-window evidence is incomplete")
        return self


class _Weighting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary: str
    secondary: str


class _Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    schema_version: int
    generated_at: datetime
    valid_through: date
    reference_horizon: str
    screening_rules_hash: str = Field(min_length=1)
    er_model_version: str = Field(min_length=1)
    primary_realized_basis: str
    secondary_realized_basis: str
    trap_basis: str
    weighting: _Weighting
    common_window: _CommonWindow
    horizons: list[_Horizon]

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if self.kind != "er-level-calibration-context" or self.schema_version != 2:
            raise ValueError("unsupported E[r] calibration context")
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at must include a timezone")
        expected_expiry = self.generated_at.astimezone(ZoneInfo("Asia/Tokyo")).date() + timedelta(
            days=45
        )
        if self.valid_through != expected_expiry:
            raise ValueError("valid_through must equal the fixed 45-day expiry")
        if (
            self.primary_realized_basis != "fy_actual_dividend_total_return"
            or self.secondary_realized_basis != "price_return_only"
            or self.trap_basis != "cohort_population_cumulative_return_excess_lte_minus_0_20"
            or self.weighting.primary != "ticker_asof_observation_equal"
            or self.weighting.secondary != "cohort_equal"
            or self.reference_horizon != "3y"
            or [item.horizon for item in self.horizons] != ["3y", "5y"]
        ):
            raise ValueError("unsupported realized-return contract")
        return self


def valid_er_distribution_context_payload(raw: object) -> bool:
    """Return whether a decoded artifact satisfies the complete consumer contract."""

    try:
        _Artifact.model_validate(raw)
    except ValidationError:
        return False
    return True
