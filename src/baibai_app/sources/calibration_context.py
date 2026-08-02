"""Fail-safe loader for the generated E[r] level display context."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .types import (
    ErLevelCalibrationContext,
    ErLevelCalibrationHorizon,
    ErLevelCalibrationQuintile,
)


class _QuintileArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    quintile: int = Field(ge=1, le=5)
    upper_er_annual: float | None
    median_predicted_er_annual: float
    median_realized_total_return_annual: float
    median_n: int = Field(gt=0)


class _HorizonArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    horizon: str
    asof_start: date
    asof_end: date
    cohort_count: int = Field(gt=0)
    quintiles: list[_QuintileArtifact]

    @model_validator(mode="after")
    def validate_bands(self) -> _HorizonArtifact:
        if self.asof_end < self.asof_start or [item.quintile for item in self.quintiles] != list(
            range(1, 6)
        ):
            raise ValueError("invalid horizon range or quintile sequence")
        cutoffs = [item.upper_er_annual for item in self.quintiles]
        if cutoffs[-1] is not None or any(value is None for value in cutoffs[:-1]):
            raise ValueError("only Q5 may have an open upper bound")
        numeric = [float(value) for value in cutoffs[:-1] if value is not None]
        if any(right <= left for left, right in pairwise(numeric)):
            raise ValueError("E[r] cutoffs must be strictly increasing")
        return self


class _ContextArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    schema_version: int
    generated_at: datetime
    valid_through: date
    reference_horizon: str
    screening_rules_hash: str = Field(min_length=1)
    er_model_version: str = Field(min_length=1)
    realized_basis: str
    horizons: list[_HorizonArtifact]

    @model_validator(mode="after")
    def validate_contract(self) -> _ContextArtifact:
        if self.kind != "er-level-calibration-context" or self.schema_version != 1:
            raise ValueError("unsupported E[r] calibration context")
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at must include a timezone")
        expected_expiry = self.generated_at.astimezone(ZoneInfo("Asia/Tokyo")).date() + timedelta(
            days=45
        )
        if self.valid_through != expected_expiry:
            raise ValueError("valid_through must equal the fixed 45-day expiry")
        if self.realized_basis != "fy_actual_dividend_total_return_annualized_absolute":
            raise ValueError("unsupported realized-return basis")
        names = [item.horizon for item in self.horizons]
        if set(names) != {"3y", "5y"} or len(names) != 2 or self.reference_horizon not in names:
            raise ValueError("reference horizon is missing or duplicated")
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
    if artifact.valid_through < current or artifact.generated_at.date() > current + timedelta(
        days=1
    ):
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
        realized_basis=artifact.realized_basis,
        horizons=tuple(
            ErLevelCalibrationHorizon(
                horizon=item.horizon,
                asof_start=item.asof_start,
                asof_end=item.asof_end,
                cohort_count=item.cohort_count,
                quintiles=tuple(
                    ErLevelCalibrationQuintile(
                        quintile=cell.quintile,
                        upper_er_annual=cell.upper_er_annual,
                        median_predicted_er_annual=cell.median_predicted_er_annual,
                        median_realized_total_return_annual=cell.median_realized_total_return_annual,
                        median_n=cell.median_n,
                    )
                    for cell in item.quintiles
                ),
            )
            for item in artifact.horizons
        ),
    )
