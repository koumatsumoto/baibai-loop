"""Thin Web DTO adapter over the shared E[r] calibration read contract."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from baibai_engine.read_api.er_calibration_context import load_er_calibration_context

from .types import (
    ErLevelCalibrationBand,
    ErLevelCalibrationBasis,
    ErLevelCalibrationContext,
    ErLevelCalibrationHorizon,
    ErLevelCalibrationStats,
)


def load_er_level_calibration_context(
    path: Path,
    *,
    expected_method_identity: tuple[str, str] | None,
    today: date | None = None,
) -> ErLevelCalibrationContext | None:
    """Adapt the shared typed artifact to the Web read DTO."""

    if expected_method_identity is None:
        return None
    rules_hash, er_model_version = expected_method_identity
    loaded = load_er_calibration_context(
        path,
        expected_rules_hash=rules_hash,
        expected_er_model_version=er_model_version,
        as_of=today or datetime.now(ZoneInfo("Asia/Tokyo")).date(),
    )
    artifact = loaded.artifact
    if artifact is None:
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
                horizon=horizon.horizon,
                asof_start=horizon.asof_start,
                asof_end=horizon.asof_end,
                cohort_count=horizon.cohort_count,
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
                    for band in horizon.bands
                ),
            )
            for horizon in artifact.horizons
        ),
    )
