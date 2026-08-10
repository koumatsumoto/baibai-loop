from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from baibai_engine.read_api import screening_calibration_method_identity
from baibai_engine.screening.calibration.panel import rules_content_hash
from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
from baibai_web.sources.calibration_context import load_er_level_calibration_context

_IDENTITY = ("rules-hash-v1", "expected-return-v1")
_ROOT = Path(__file__).resolve().parents[2]


def _artifact() -> dict[str, object]:
    def stats(index: int) -> dict[str, object]:
        return {
            "median": -0.1 + index * 0.06,
            "q25": -0.12 + index * 0.06,
            "q10": -0.14 + index * 0.06,
            "trap_rate": 0.2,
            "n": 200,
        }

    artifact: dict[str, object] = {
        "kind": "er-level-calibration-context",
        "schema_version": 2,
        "generated_at": "2026-08-02T12:00:00+09:00",
        "valid_through": "2026-09-16",
        "reference_horizon": "3y",
        "screening_rules_hash": _IDENTITY[0],
        "er_model_version": _IDENTITY[1],
        "primary_realized_basis": "fy_actual_dividend_total_return",
        "secondary_realized_basis": "price_return_only",
        "trap_basis": "cohort_population_cumulative_return_excess_lte_minus_0_20",
        "weighting": {
            "primary": "ticker_asof_observation_equal",
            "secondary": "cohort_equal",
        },
        "common_window": {
            "asof_start": "2020-01-31",
            "asof_end": "2021-05-31",
            "cohort_count": 11,
        },
        "horizons": [
            {
                "horizon": horizon,
                "asof_start": "2020-01-31",
                "asof_end": "2021-05-31",
                "cohort_count": 11,
                "bands": [
                    {
                        "band_id": f"q{index + 1}",
                        "quintile": index + 1,
                        "lower_er_annual": None if index == 0 else -0.06 + index * 0.02,
                        "upper_er_annual": None if index == 4 else -0.04 + index * 0.02,
                        "median_predicted_er_annual": -0.05 + index * 0.025,
                        "cohort_count": 11,
                        "median_n": 200,
                        "bases": [
                            {
                                "basis": basis,
                                "ticker_equal": stats(index),
                                "cohort_equal": stats(index),
                            }
                            for basis in (
                                "fy_actual_dividend_total_return",
                                "price_return_only",
                            )
                        ],
                    }
                    for index in range(5)
                ]
                + [
                    {
                        "band_id": "er_gte_8_5pct",
                        "quintile": None,
                        "lower_er_annual": 0.085,
                        "upper_er_annual": None,
                        "median_predicted_er_annual": 0.1,
                        "cohort_count": 11,
                        "median_n": 20,
                        "bases": [
                            {
                                "basis": basis,
                                "ticker_equal": stats(4),
                                "cohort_equal": stats(4),
                            }
                            for basis in (
                                "fy_actual_dividend_total_return",
                                "price_return_only",
                            )
                        ],
                    }
                ],
            }
            for horizon in ("3y", "5y")
        ],
    }
    common = artifact["common_window"]
    assert isinstance(common, dict)
    common["horizons"] = artifact["horizons"]
    return artifact


def _write(path: Path, payload: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_context_loader_accepts_current_generated_artifact(tmp_path: Path) -> None:
    path = tmp_path / "context.yaml"
    _write(path, _artifact())

    context = load_er_level_calibration_context(
        path, expected_method_identity=_IDENTITY, today=date(2026, 8, 31)
    )

    assert context is not None
    assert context.reference_horizon == "3y"
    assert context.horizons[0].bands[4].upper_er_annual is None


def test_current_method_identity_matches_production_panel_contract() -> None:
    identity = screening_calibration_method_identity(_ROOT)

    assert identity is not None
    assert identity[0] == rules_content_hash(load_screening_rules(_ROOT / DEFAULT_RULES_PATH))


def test_context_loader_hides_missing_stale_or_invalid_artifact(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"
    assert (
        load_er_level_calibration_context(
            missing, expected_method_identity=_IDENTITY, today=date(2026, 8, 2)
        )
        is None
    )

    path = tmp_path / "context.yaml"
    _write(path, _artifact())
    assert (
        load_er_level_calibration_context(
            path, expected_method_identity=_IDENTITY, today=date(2026, 9, 17)
        )
        is None
    )

    invalid = _artifact()
    horizons = invalid["horizons"]
    assert isinstance(horizons, list)
    first = horizons[0]
    assert isinstance(first, dict)
    bands = first["bands"]
    assert isinstance(bands, list)
    bands[0]["upper_er_annual"] = None
    _write(path, invalid)
    assert (
        load_er_level_calibration_context(
            path, expected_method_identity=_IDENTITY, today=date(2026, 8, 2)
        )
        is None
    )

    gap = _artifact()
    gap_horizons = gap["horizons"]
    assert isinstance(gap_horizons, list)
    gap_first = gap_horizons[0]
    assert isinstance(gap_first, dict)
    gap_bands = gap_first["bands"]
    assert isinstance(gap_bands, list)
    gap_bands[1]["lower_er_annual"] = -0.01
    _write(path, gap)
    assert (
        load_er_level_calibration_context(
            path, expected_method_identity=_IDENTITY, today=date(2026, 8, 2)
        )
        is None
    )

    partial = _artifact()
    partial_horizons = partial["horizons"]
    assert isinstance(partial_horizons, list)
    partial["horizons"] = partial_horizons[:1]
    _write(path, partial)
    assert (
        load_er_level_calibration_context(
            path, expected_method_identity=_IDENTITY, today=date(2026, 8, 2)
        )
        is None
    )


def test_context_loader_hides_provenance_mismatch_and_overlong_ttl(tmp_path: Path) -> None:
    path = tmp_path / "context.yaml"
    _write(path, _artifact())

    assert (
        load_er_level_calibration_context(
            path,
            expected_method_identity=("different-rules", _IDENTITY[1]),
            today=date(2026, 8, 2),
        )
        is None
    )

    overlong = _artifact()
    overlong["valid_through"] = "2096-09-16"
    _write(path, overlong)
    assert (
        load_er_level_calibration_context(
            path, expected_method_identity=_IDENTITY, today=date(2026, 8, 2)
        )
        is None
    )


def test_context_loader_hides_unsupported_reference_and_future_artifact(tmp_path: Path) -> None:
    path = tmp_path / "context.yaml"
    unsupported_reference = _artifact()
    unsupported_reference["reference_horizon"] = "5y"
    _write(path, unsupported_reference)
    assert (
        load_er_level_calibration_context(
            path, expected_method_identity=_IDENTITY, today=date(2026, 8, 2)
        )
        is None
    )

    future = _artifact()
    future["generated_at"] = "2026-08-03T00:00:00+09:00"
    future["valid_through"] = "2026-09-17"
    _write(path, future)
    assert (
        load_er_level_calibration_context(
            path, expected_method_identity=_IDENTITY, today=date(2026, 8, 2)
        )
        is None
    )
