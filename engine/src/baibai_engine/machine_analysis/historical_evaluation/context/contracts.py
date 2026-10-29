"""保存済みE[r]較正文脈の構造と分母・分布の整合契約。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from itertools import pairwise
from math import isclose
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ER_CALIBRATION_CONTEXT_KIND = "er-level-calibration-context"
ER_CALIBRATION_CONTEXT_SCHEMA_VERSION = 4
ER_CALIBRATION_CONTEXT_VALID_DAYS = 45
ER_CALIBRATION_PRIMARY_REALIZED_BASIS = "fy_actual_dividend_total_return"
ER_CALIBRATION_SECONDARY_REALIZED_BASIS = "price_return_only"
ER_CALIBRATION_TRAP_BASIS = "cohort_population_cumulative_return_excess_lte_minus_0_20"
ER_CALIBRATION_PRIMARY_WEIGHTING = "ticker_asof_observation_equal"
ER_CALIBRATION_SECONDARY_WEIGHTING = "cohort_equal"
ER_CALIBRATION_REFERENCE_HORIZON = "3y"
ER_CALIBRATION_HORIZONS = ("3y", "5y")
ER_CALIBRATION_BAND_IDS = ("q1", "q2", "q3", "q4", "q5", "er_gte_8_5pct")
# 機械E[r]の固定較正帯。L3の案件別要求率へ追随させない (#1481)。
# docs/reference/estimate-calibration.md
HURDLE_ER_ANNUAL = 0.085


class ErCalibrationStats(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

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


class ErCalibrationCoverage(BaseModel):
    """Observed outcomes within the declared finite-E[r] population."""

    model_config = ConfigDict(extra="forbid", strict=True)

    declared_n: int = Field(ge=0)
    observed_n: int = Field(ge=0)
    missing_n: int = Field(ge=0)
    missing_reason_counts: dict[str, int]

    @model_validator(mode="after")
    def validate_partition(self) -> Self:
        if self.declared_n != self.observed_n + self.missing_n:
            raise ValueError("declared population must equal observed plus missing")
        if any(not key or value <= 0 for key, value in self.missing_reason_counts.items()):
            raise ValueError("missing reasons must have positive counts")
        if sum(self.missing_reason_counts.values()) != self.missing_n:
            raise ValueError("missing reasons must account for every missing observation")
        return self


class ErCalibrationBasis(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    basis: Literal["fy_actual_dividend_total_return", "price_return_only"]
    coverage: ErCalibrationCoverage
    observed_cohort_count: int = Field(ge=0)
    median_n: int = Field(ge=0)
    ticker_equal: ErCalibrationStats | None
    cohort_equal: ErCalibrationStats | None

    @model_validator(mode="after")
    def validate_observations(self) -> Self:
        if self.coverage.observed_n == 0:
            if self.ticker_equal is not None or self.cohort_equal is not None:
                raise ValueError("an unobserved basis must have null distributions")
            if self.observed_cohort_count or self.median_n:
                raise ValueError("an unobserved basis cannot claim observed cohorts")
        elif (
            self.ticker_equal is None
            or self.cohort_equal is None
            or self.ticker_equal.n != self.coverage.observed_n
            or self.observed_cohort_count == 0
        ):
            raise ValueError("basis distributions must match observed coverage")
        return self


class ErCalibrationBand(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    band_id: Literal["q1", "q2", "q3", "q4", "q5", "er_gte_8_5pct"]
    quintile: int | None = Field(ge=1, le=5)
    lower_er_annual: float | None
    upper_er_annual: float | None
    median_predicted_er_annual: float | None
    cohort_count: int = Field(gt=0)
    median_n: int = Field(ge=0)
    bases: list[ErCalibrationBasis]

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        expected_id = f"q{self.quintile}" if self.quintile is not None else "er_gte_8_5pct"
        if self.band_id != expected_id:
            raise ValueError("band id and quintile disagree")
        if self.quintile is None and self.lower_er_annual != HURDLE_ER_ANNUAL:
            raise ValueError("hurdle band must start at 8.5%")
        if [item.basis for item in self.bases] != [
            ER_CALIBRATION_PRIMARY_REALIZED_BASIS,
            ER_CALIBRATION_SECONDARY_REALIZED_BASIS,
        ]:
            raise ValueError("realized-return bases are missing, duplicated, or reordered")
        declared = {item.coverage.declared_n for item in self.bases}
        if len(declared) != 1:
            raise ValueError("both bases must retain the same declared prediction band")
        if (next(iter(declared)) == 0) != (self.median_predicted_er_annual is None):
            raise ValueError("prediction median must match the declared band")
        if any(item.observed_cohort_count > self.cohort_count for item in self.bases):
            raise ValueError("observed cohorts cannot exceed declared cohorts")
        return self


class ErCalibrationHorizon(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    horizon: Literal["3y", "5y"]
    asof_start: date
    asof_end: date
    cohort_count: int = Field(gt=0)
    bands: list[ErCalibrationBand]

    @field_validator("asof_start", "asof_end", mode="before")
    @classmethod
    def parse_date(cls, value: object) -> object:
        return date.fromisoformat(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_bands(self) -> Self:
        if self.asof_end < self.asof_start or [item.band_id for item in self.bands] != list(
            ER_CALIBRATION_BAND_IDS
        ):
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


class ErCalibrationCommonWindow(BaseModel):
    """Nested wire shape; the root validates equality with declared horizons."""

    model_config = ConfigDict(extra="forbid", strict=True)

    asof_start: date | None
    asof_end: date | None
    cohort_count: int = Field(ge=0)
    horizons: list[ErCalibrationHorizon]

    @field_validator("asof_start", "asof_end", mode="before")
    @classmethod
    def parse_optional_date(cls, value: object) -> object:
        return date.fromisoformat(value) if isinstance(value, str) else value


class ErCalibrationWeighting(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    primary: Literal["ticker_asof_observation_equal"]
    secondary: Literal["cohort_equal"]


class ErCalibrationContextArtifact(BaseModel):
    """The only structural contract for the published E[r] context artifact."""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["er-level-calibration-context"]
    schema_version: Literal[4]
    interpretation: Literal["observed_distribution_only"]
    required_asofs: list[str]
    evidence_complete: bool
    evidence_blocking_reasons: list[str]
    generated_at: datetime
    valid_through: date
    reference_horizon: Literal["3y"]
    screening_rules_hash: str = Field(min_length=1)
    er_model_version: str = Field(min_length=1)
    primary_realized_basis: Literal["fy_actual_dividend_total_return"]
    secondary_realized_basis: Literal["price_return_only"]
    trap_basis: Literal["cohort_population_cumulative_return_excess_lte_minus_0_20"]
    weighting: ErCalibrationWeighting
    common_window: ErCalibrationCommonWindow
    horizons: list[ErCalibrationHorizon]

    @field_validator("generated_at", mode="before")
    @classmethod
    def parse_generated_at(cls, value: object) -> object:
        return datetime.fromisoformat(value) if isinstance(value, str) else value

    @field_validator("valid_through", mode="before")
    @classmethod
    def parse_valid_through(cls, value: object) -> object:
        return date.fromisoformat(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at must include a timezone")
        expected_expiry = self.generated_at.astimezone(ZoneInfo("Asia/Tokyo")).date() + timedelta(
            days=ER_CALIBRATION_CONTEXT_VALID_DAYS
        )
        if self.valid_through != expected_expiry:
            raise ValueError("valid_through must equal the fixed 45-day expiry")
        if [item.horizon for item in self.horizons] != list(ER_CALIBRATION_HORIZONS):
            raise ValueError("E[r] context must contain 3y and 5y horizons in order")
        if not self.required_asofs or self.required_asofs != sorted(set(self.required_asofs)):
            raise ValueError("required dates must be explicit, unique and ordered")
        required_dates = [date.fromisoformat(value) for value in self.required_asofs]
        if self.evidence_complete and self.evidence_blocking_reasons:
            raise ValueError("complete evidence cannot have blocking reasons")
        if any(
            item.cohort_count != len(required_dates)
            or item.asof_start != required_dates[0]
            or item.asof_end != required_dates[-1]
            for item in self.horizons
        ):
            raise ValueError("every horizon must retain the declared dates")
        for reference, other in zip(self.horizons[0].bands, self.horizons[1].bands, strict=True):
            if (
                reference.lower_er_annual,
                reference.upper_er_annual,
                reference.median_predicted_er_annual,
                reference.median_n,
                reference.bases[0].coverage.declared_n,
            ) != (
                other.lower_er_annual,
                other.upper_er_annual,
                other.median_predicted_er_annual,
                other.median_n,
                other.bases[0].coverage.declared_n,
            ):
                raise ValueError("horizons must share the same declared prediction bands")
        if (
            self.common_window.cohort_count != len(required_dates)
            or self.common_window.asof_start != required_dates[0]
            or self.common_window.asof_end != required_dates[-1]
            or self.common_window.horizons != self.horizons
        ):
            raise ValueError("common window must retain the same declared distributions")
        return self


__all__ = [
    "ER_CALIBRATION_BAND_IDS",
    "ER_CALIBRATION_CONTEXT_KIND",
    "ER_CALIBRATION_CONTEXT_SCHEMA_VERSION",
    "ER_CALIBRATION_CONTEXT_VALID_DAYS",
    "ER_CALIBRATION_HORIZONS",
    "ER_CALIBRATION_PRIMARY_REALIZED_BASIS",
    "ER_CALIBRATION_PRIMARY_WEIGHTING",
    "ER_CALIBRATION_REFERENCE_HORIZON",
    "ER_CALIBRATION_SECONDARY_REALIZED_BASIS",
    "ER_CALIBRATION_SECONDARY_WEIGHTING",
    "ER_CALIBRATION_TRAP_BASIS",
    "HURDLE_ER_ANNUAL",
    "ErCalibrationBand",
    "ErCalibrationBasis",
    "ErCalibrationCommonWindow",
    "ErCalibrationContextArtifact",
    "ErCalibrationCoverage",
    "ErCalibrationHorizon",
    "ErCalibrationStats",
    "ErCalibrationWeighting",
]
