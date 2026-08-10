"""Fail-safe loader for the generated E[r] level display context."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from itertools import pairwise
from math import isclose
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .types import (
    ErLevelCalibrationBand,
    ErLevelCalibrationBasis,
    ErLevelCalibrationContext,
    ErLevelCalibrationHorizon,
    ErLevelCalibrationStats,
)


class _StatsArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    median: float
    q25: float
    q10: float
    trap_rate: float = Field(ge=0, le=1)
    n: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_quantiles(self) -> _StatsArtifact:
        if not self.q10 <= self.q25 <= self.median:
            raise ValueError("realized-return quantiles must be ordered")
        return self


class _BasisArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    basis: str
    ticker_equal: _StatsArtifact
    cohort_equal: _StatsArtifact


class _BandArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    band_id: str
    quintile: int | None = Field(ge=1, le=5)
    lower_er_annual: float | None
    upper_er_annual: float | None
    median_predicted_er_annual: float
    cohort_count: int = Field(gt=0)
    median_n: int = Field(gt=0)
    bases: list[_BasisArtifact]

    @model_validator(mode="after")
    def validate_identity(self) -> _BandArtifact:
        expected_id = f"q{self.quintile}" if self.quintile is not None else "er_gte_8_5pct"
        if self.band_id != expected_id:
            raise ValueError("band id and quintile disagree")
        if self.quintile is None and self.lower_er_annual != 0.085:
            raise ValueError("hurdle band must start at 8.5%")
        if self.quintile is not None and self.quintile > 1 and self.lower_er_annual is None:
            raise ValueError("closed quintile lower bound is missing")
        names = [item.basis for item in self.bases]
        if names != ["fy_actual_dividend_total_return", "price_return_only"]:
            raise ValueError("realized-return bases are missing, duplicated, or reordered")
        return self


class _HorizonArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    horizon: str
    asof_start: date
    asof_end: date
    cohort_count: int = Field(gt=0)
    bands: list[_BandArtifact]

    @model_validator(mode="after")
    def validate_bands(self) -> _HorizonArtifact:
        if self.asof_end < self.asof_start or [item.band_id for item in self.bands] != [
            "q1",
            "q2",
            "q3",
            "q4",
            "q5",
            "er_gte_8_5pct",
        ]:
            raise ValueError("invalid horizon range or quintile sequence")
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


class _CommonWindowArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asof_start: date | None
    asof_end: date | None
    cohort_count: int = Field(ge=0)
    horizons: list[_HorizonArtifact]

    @model_validator(mode="after")
    def validate_window(self) -> _CommonWindowArtifact:
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


class _WeightingArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary: str
    secondary: str


class _ContextArtifact(BaseModel):
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
    weighting: _WeightingArtifact
    common_window: _CommonWindowArtifact
    horizons: list[_HorizonArtifact]

    @model_validator(mode="after")
    def validate_contract(self) -> _ContextArtifact:
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
        ):
            raise ValueError("unsupported realized-return basis")
        if self.reference_horizon != "3y" or [item.horizon for item in self.horizons] != [
            "3y",
            "5y",
        ]:
            raise ValueError("reference horizon or horizon sequence is unsupported")
        return self


def load_er_level_calibration_context(
    path: Path,
    *,
    expected_method_identity: tuple[str, str] | None,
    today: date | None = None,
) -> ErLevelCalibrationContext | None:
    """Return a validated current context, or hide it when any contract is uncertain."""

    if not path.is_file() or expected_method_identity is None:
        return None
    try:
        artifact = _ContextArtifact.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError):
        return None
    current = today or datetime.now(ZoneInfo("Asia/Tokyo")).date()
    generated_on = artifact.generated_at.astimezone(ZoneInfo("Asia/Tokyo")).date()
    if artifact.valid_through < current or generated_on > current:
        return None
    expected_rules_hash, expected_er_model_version = expected_method_identity
    if (
        artifact.screening_rules_hash != expected_rules_hash
        or artifact.er_model_version != expected_er_model_version
    ):
        return None
    return ErLevelCalibrationContext(
        generated_at=artifact.generated_at,
        valid_through=artifact.valid_through,
        reference_horizon=artifact.reference_horizon,
        screening_rules_hash=artifact.screening_rules_hash,
        er_model_version=artifact.er_model_version,
        primary_realized_basis=artifact.primary_realized_basis,
        secondary_realized_basis=artifact.secondary_realized_basis,
        trap_basis=artifact.trap_basis,
        horizons=tuple(
            ErLevelCalibrationHorizon(
                horizon=item.horizon,
                asof_start=item.asof_start,
                asof_end=item.asof_end,
                cohort_count=item.cohort_count,
                bands=tuple(
                    ErLevelCalibrationBand(
                        band_id=band.band_id,
                        quintile=band.quintile,
                        lower_er_annual=band.lower_er_annual,
                        upper_er_annual=band.upper_er_annual,
                        median_predicted_er_annual=band.median_predicted_er_annual,
                        cohort_count=band.cohort_count,
                        median_n=band.median_n,
                        bases=tuple(
                            ErLevelCalibrationBasis(
                                basis=basis.basis,
                                ticker_equal=ErLevelCalibrationStats(
                                    median=basis.ticker_equal.median,
                                    q25=basis.ticker_equal.q25,
                                    q10=basis.ticker_equal.q10,
                                    trap_rate=basis.ticker_equal.trap_rate,
                                    n=basis.ticker_equal.n,
                                ),
                                cohort_equal=ErLevelCalibrationStats(
                                    median=basis.cohort_equal.median,
                                    q25=basis.cohort_equal.q25,
                                    q10=basis.cohort_equal.q10,
                                    trap_rate=basis.cohort_equal.trap_rate,
                                    n=basis.cohort_equal.n,
                                ),
                            )
                            for basis in band.bases
                        ),
                    )
                    for band in item.bands
                ),
            )
            for item in artifact.horizons
        ),
    )
