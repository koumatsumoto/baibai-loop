from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from baibai_app.sources.calibration_context import load_er_level_calibration_context
from baibai_engine.read_api import screening_calibration_method_identity
from baibai_engine.screening.calibration.panel import rules_content_hash
from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules

_IDENTITY = ("rules-hash-v1", "expected-return-v1")
_ROOT = Path(__file__).resolve().parents[1]


def _artifact() -> dict[str, object]:
    return {
        "kind": "er-level-calibration-context",
        "schema_version": 1,
        "generated_at": "2026-08-02T12:00:00+09:00",
        "valid_through": "2026-09-16",
        "reference_horizon": "3y",
        "screening_rules_hash": _IDENTITY[0],
        "er_model_version": _IDENTITY[1],
        "realized_basis": "fy_actual_dividend_total_return_annualized_absolute",
        "horizons": [
            {
                "horizon": horizon,
                "asof_start": "2020-01-31",
                "asof_end": "2021-05-31",
                "cohort_count": 11,
                "quintiles": [
                    {
                        "quintile": index + 1,
                        "upper_er_annual": None if index == 4 else -0.04 + index * 0.02,
                        "median_predicted_er_annual": -0.05 + index * 0.025,
                        "median_realized_total_return_annual": -0.1 + index * 0.06,
                        "median_n": 200,
                    }
                    for index in range(5)
                ],
            }
            for horizon in ("3y", "5y")
        ],
    }


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
    assert context.horizons[0].quintiles[4].upper_er_annual is None


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
    quintiles = first["quintiles"]
    assert isinstance(quintiles, list)
    quintiles[0]["upper_er_annual"] = None
    _write(path, invalid)
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
