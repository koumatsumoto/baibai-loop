from __future__ import annotations

from pathlib import Path
from statistics import median

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
STUDY = ROOT / "reports/studies/2026-08-24-earnings-power-v1"


def _yaml(path: Path) -> dict[str, object]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_inconclusive_policy_has_no_active_execution_surface() -> None:
    assert not (ROOT / "method/screening/selection-policies/earnings-power-v1.yaml").exists()
    assert not (ROOT / "engine/src/baibai_engine/screening/selection/earnings_replay.py").exists()
    assert not (ROOT / "tools/studies/earnings_power_v1_replay.py").exists()


def test_frozen_evidence_mechanically_supports_inconclusive_verdict() -> None:
    policy = _yaml(STUDY / "frozen-policy.yaml")
    replay = _yaml(STUDY / "historical-replay.yaml")
    assert policy["selection_policy_id"] == "earnings-power-v1"
    assert policy["metric"] == "normalized_per_3fy"
    assert replay["verdict"] == "inconclusive"
    assert replay["policy"]["maximum_normalized_per_3fy"] == 12.0  # type: ignore[index]

    geometry = replay["geometry"]
    assert isinstance(geometry, list)
    assert len(geometry) == 81
    verdict_inputs = replay["verdict_inputs"]
    assert isinstance(verdict_inputs, dict)
    horizons = replay["horizons"]
    assert isinstance(horizons, dict)
    assert set(horizons) == {"3y", "5y"}

    comparisons: list[dict[str, float]] = []
    total_return_coverages: list[float] = []
    for horizon in ("3y", "5y"):
        result = horizons[horizon]
        assert isinstance(result, dict)
        assert result["eligible_cohort_count"] >= 12
        bases = result["bases"]
        assert isinstance(bases, dict)
        for basis in ("price_return", "total_return"):
            basis_result = bases[basis]
            assert isinstance(basis_result, dict)
            difference = basis_result["cohort_equal_median_difference"]
            assert isinstance(difference, dict)
            comparisons.append(
                {name: float(difference[name]) for name in ("as_reported", "neutral", "failure")}
            )
            if basis == "total_return":
                for lane in ("earnings_power", "value_carry"):
                    lane_result = basis_result[lane]
                    assert isinstance(lane_result, dict)
                    total_return_coverages.append(float(lane_result["coverage"]))

    negative_comparison = any(all(values[name] < 0 for name in values) for values in comparisons)
    sensitivity_sign_split = any(
        min(values.values()) < 0 <= max(values.values()) for values in comparisons
    )
    total_return_coverage_below_floor = any(coverage < 0.75 for coverage in total_return_coverages)
    median_alt_only_count = median(row["alt_only_count"] for row in geometry)
    median_sector_concentration = median(row["earnings_sector_concentration"] for row in geometry)
    all_as_reported_nonnegative = all(values["as_reported"] >= 0 for values in comparisons)
    if negative_comparison:
        derived_verdict = "negative"
    elif sensitivity_sign_split or total_return_coverage_below_floor:
        derived_verdict = "inconclusive"
    elif (
        all_as_reported_nonnegative
        and median_alt_only_count >= 5
        and median_sector_concentration <= 0.5
    ):
        derived_verdict = "eligible_for_shadow"
    else:
        derived_verdict = "inconclusive"

    assert verdict_inputs == {
        "negative_comparison": negative_comparison,
        "sensitivity_sign_split": sensitivity_sign_split,
        "total_return_coverage_below_floor": total_return_coverage_below_floor,
        "median_alt_only_count": median_alt_only_count,
        "median_sector_concentration": pytest.approx(median_sector_concentration),
        "all_as_reported_differences_nonnegative": all_as_reported_nonnegative,
    }
    assert replay["verdict"] == derived_verdict == "inconclusive"
