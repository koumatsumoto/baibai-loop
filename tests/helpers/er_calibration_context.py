"""Synthetic context evidence independent of the latest published artifact."""

from copy import deepcopy
from typing import Any


def context_artifact(*, rules_hash: str = "old-rules") -> dict[str, Any]:
    stats = {"median": 0.1, "q25": 0.0, "q10": -0.1, "trap_rate": 0.2, "n": 100}
    bases = [
        {"basis": basis, "ticker_equal": deepcopy(stats), "cohort_equal": deepcopy(stats)}
        for basis in ("fy_actual_dividend_total_return", "price_return_only")
    ]
    bounds = (None, -0.02, 0.0, 0.02, 0.04, None)
    bands = [
        {
            "band_id": f"q{index + 1}",
            "quintile": index + 1,
            "lower_er_annual": bounds[index],
            "upper_er_annual": bounds[index + 1],
            "median_predicted_er_annual": index * 0.02 - 0.03,
            "cohort_count": 1,
            "median_n": 100,
            "bases": deepcopy(bases),
        }
        for index in range(5)
    ]
    bands.append(
        {
            "band_id": "er_gte_8_5pct",
            "quintile": None,
            "lower_er_annual": 0.085,
            "upper_er_annual": None,
            "median_predicted_er_annual": 0.1,
            "cohort_count": 1,
            "median_n": 100,
            "bases": deepcopy(bases),
        }
    )
    horizons = [
        {
            "horizon": horizon,
            "asof_start": "2020-01-31",
            "asof_end": "2020-01-31",
            "cohort_count": 1,
            "bands": deepcopy(bands),
        }
        for horizon in ("3y", "5y")
    ]
    return {
        "kind": "er-level-calibration-context",
        "schema_version": 2,
        "generated_at": "2026-10-01T18:00:00+09:00",
        "valid_through": "2026-11-15",
        "reference_horizon": "3y",
        "screening_rules_hash": rules_hash,
        "er_model_version": "expected-return-v1",
        "primary_realized_basis": "fy_actual_dividend_total_return",
        "secondary_realized_basis": "price_return_only",
        "trap_basis": "cohort_population_cumulative_return_excess_lte_minus_0_20",
        "weighting": {"primary": "ticker_asof_observation_equal", "secondary": "cohort_equal"},
        "common_window": {
            "asof_start": "2020-01-31",
            "asof_end": "2020-01-31",
            "cohort_count": 1,
            "horizons": deepcopy(horizons),
        },
        "horizons": horizons,
    }
