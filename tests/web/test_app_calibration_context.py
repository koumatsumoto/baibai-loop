from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path

import yaml
from tests.helpers.er_calibration_context import context_artifact

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.read_api import screening_calibration_method_identity
from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
from baibai_engine.screening.rules_identity import production_rules_contract_hash
from baibai_web.sources.calibration_context import load_er_level_calibration_context

_ROOT = Path(__file__).resolve().parents[2]
_PUBLISHED = _ROOT / "reports/published/er-level-calibration-latest.yaml"


def test_retained_schema_2_is_withheld_even_for_matching_historical_identity() -> None:
    raw = safe_load(_PUBLISHED.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    identity = (str(raw["screening_rules_hash"]), str(raw["er_model_version"]))

    context = load_er_level_calibration_context(
        _PUBLISHED,
        expected_method_identity=identity,
        today=date.fromisoformat(str(raw["generated_at"])[:10]),
    )

    assert context is None


def test_schema_3_context_reaches_web_for_matching_identity(tmp_path) -> None:
    raw = context_artifact()
    path = tmp_path / "fixed-context.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    context = load_er_level_calibration_context(
        path,
        expected_method_identity=(raw["screening_rules_hash"], raw["er_model_version"]),
        today=date.fromisoformat(raw["generated_at"][:10]),
    )
    assert context is not None
    assert context.schema_version == 3
    assert [horizon.horizon for horizon in context.horizons] == ["3y", "5y"]


def test_new_comparison_population_does_not_reuse_retained_context(tmp_path) -> None:
    raw = context_artifact()
    path = tmp_path / "old-context.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    identity = ("new-rules", str(raw["er_model_version"]))
    assert (
        load_er_level_calibration_context(
            path,
            expected_method_identity=identity,
            today=date.fromisoformat(str(raw["generated_at"])[:10]),
        )
        is None
    )


def test_invalid_artifact_maps_to_null(tmp_path: Path) -> None:
    raw = safe_load(_PUBLISHED.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    invalid = deepcopy(raw)
    invalid["unknown"] = "value"
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(invalid, sort_keys=False), encoding="utf-8")

    context = load_er_level_calibration_context(
        path,
        expected_method_identity=(
            str(raw["screening_rules_hash"]),
            str(raw["er_model_version"]),
        ),
        today=date.fromisoformat(str(raw["generated_at"])[:10]),
    )

    assert context is None


def test_current_method_identity_matches_production_screening_contract() -> None:
    identity = screening_calibration_method_identity(_ROOT)

    assert identity is not None
    assert identity[0] == production_rules_contract_hash(
        load_screening_rules(_ROOT / DEFAULT_RULES_PATH).model_dump_json()
    )
