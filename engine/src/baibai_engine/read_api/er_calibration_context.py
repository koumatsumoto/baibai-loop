"""Measure T1/T2 E[r] evidence through the shared typed artifact contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import isfinite
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import ValidationError

from baibai_engine.foundation.er_calibration_context import (
    HURDLE_ER_ANNUAL,
    ErCalibrationBand,
    ErCalibrationContextArtifact,
)
from baibai_engine.foundation.yaml_io import safe_load

ErCalibrationUnavailableReason = Literal[
    "missing_artifact",
    "invalid_artifact",
    "expired",
    "rules_identity_mismatch",
    "er_model_identity_mismatch",
]


@dataclass(frozen=True, slots=True)
class ErCalibrationContextLoad:
    artifact: ErCalibrationContextArtifact | None
    unavailable_reason: ErCalibrationUnavailableReason | None


def load_er_calibration_context(
    path: Path,
    *,
    expected_rules_hash: str,
    expected_er_model_version: str,
    as_of: date,
) -> ErCalibrationContextLoad:
    """Load one context and preserve the exact reason it cannot be used."""

    if not path.is_file():
        return ErCalibrationContextLoad(None, "missing_artifact")
    try:
        artifact = ErCalibrationContextArtifact.model_validate(
            safe_load(path.read_text(encoding="utf-8"))
        )
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError):
        return ErCalibrationContextLoad(None, "invalid_artifact")
    generated_on = artifact.generated_at.astimezone(ZoneInfo("Asia/Tokyo")).date()
    if generated_on > as_of:
        return ErCalibrationContextLoad(None, "invalid_artifact")
    if artifact.valid_through < as_of:
        return ErCalibrationContextLoad(None, "expired")
    if artifact.screening_rules_hash != expected_rules_hash:
        return ErCalibrationContextLoad(None, "rules_identity_mismatch")
    if artifact.er_model_version != expected_er_model_version:
        return ErCalibrationContextLoad(None, "er_model_identity_mismatch")
    return ErCalibrationContextLoad(artifact, None)


def candidate_er_band_context(
    artifact: ErCalibrationContextArtifact, er_annual: float
) -> tuple[dict[str, object], ...]:
    """Allocate one ratio-valued candidate E[r] to the artifact's validated bands."""

    if not isfinite(er_annual):
        raise ValueError("candidate E[r] must be finite")
    horizons: list[dict[str, object]] = []
    for horizon in artifact.horizons:
        quintile = next(
            band
            for band in horizon.bands[:5]
            if band.upper_er_annual is None or er_annual <= band.upper_er_annual
        )
        hurdle = horizon.bands[-1] if er_annual >= HURDLE_ER_ANNUAL else None
        horizons.append(
            {
                "horizon": horizon.horizon,
                "cohort_count": horizon.cohort_count,
                "bands": [_band_summary(band) for band in (quintile, hurdle) if band is not None],
            }
        )
    return tuple(horizons)


def _band_summary(band: ErCalibrationBand) -> dict[str, object]:
    primary = band.bases[0].ticker_equal
    return {
        "band_id": band.band_id,
        "lower_er_annual": band.lower_er_annual,
        "upper_er_annual": band.upper_er_annual,
        "median_predicted_er_annual": band.median_predicted_er_annual,
        "cohort_count": band.cohort_count,
        "median_n": band.median_n,
        "realized_total_return_ticker_equal": primary.model_dump(mode="json"),
    }
