from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from tests.helpers.er_calibration_context import context_artifact
from tests.helpers.research_selection import selection
from tests.helpers.screening_run import stock_analysis

from baibai_engine.foundation.repository_layout import ER_LEVEL_CALIBRATION_CONTEXT_PATH
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.investment_judgment.calibration_context import research_calibration_context
from baibai_engine.investment_judgment.selection import selection_sha256
from baibai_engine.investment_judgment.workspace_cli import build_parser
from baibai_engine.machine_analysis.screening.rule_config import (
    DEFAULT_RULES_PATH,
    load_screening_rules,
)
from baibai_engine.machine_analysis.screening.rules_identity import production_rules_contract_hash
from baibai_engine.read_api import screening_calibration_method_identity
from baibai_web.sources.calibration_context import load_er_level_calibration_context

_ROOT = Path(__file__).resolve().parents[2]
_PUBLISHED = _ROOT / ER_LEVEL_CALIBRATION_CONTEXT_PATH
_RETAINED = _ROOT / "tests/fixtures/valuation/calibration/retained-schema2-context.yaml"


@pytest.mark.parametrize(
    ("condition", "reason"),
    [
        ("matching", None),
        ("future", "invalid_artifact"),
        ("expired", "expired"),
        ("rules", "rules_identity_mismatch"),
        ("model", "er_model_identity_mismatch"),
    ],
)
def test_published_default_preserves_web_and_research_diagnostics(
    tmp_path, monkeypatch, condition, reason
):
    monkeypatch.chdir(_ROOT)
    raw = safe_load(_PUBLISHED.read_text(encoding="utf-8"))
    generated = datetime.fromisoformat(raw["generated_at"])
    as_of = generated.date()
    if condition == "future":
        as_of -= timedelta(days=1)
    elif condition == "expired":
        as_of = date.fromisoformat(raw["valid_through"]) + timedelta(days=1)
    identity = (
        "different" if condition == "rules" else raw["screening_rules_hash"],
        "different" if condition == "model" else raw["er_model_version"],
    )
    selected = selection(
        now=generated,
        tickers=("1234",),
        runs=tmp_path / "runs",
        rows=[stock_analysis("1234", name="合成会社", metrics={"er_annual": 0.09})],
    )
    source = selected.inputs.analysis.model_copy(
        update={"screening_rules_hash": identity[0], "er_model_version": identity[1]}
    )
    selected = selected.model_copy(
        update={"inputs": selected.inputs.model_copy(update={"analysis": source})}
    )
    before = selection_sha256(selected), _PUBLISHED.read_bytes()
    copy = tmp_path / "context.yaml"
    copy.write_bytes(before[1])
    default = build_parser().parse_args(["calibration-context", "--operation-id", "saved"])
    assert default.context_path == ER_LEVEL_CALIBRATION_CONTEXT_PATH

    web = load_er_level_calibration_context(
        _ROOT / default.context_path, expected_method_identity=identity, today=as_of
    )
    assert web == load_er_level_calibration_context(
        copy, expected_method_identity=identity, today=as_of
    )
    research = research_calibration_context(
        selected, context_path=default.context_path, as_of=as_of
    )
    assert research == research_calibration_context(selected, context_path=copy, as_of=as_of)
    if reason is None:
        assert web is not None
        assert research["context"]["evidence_complete"] is raw["evidence_complete"]
        assert (
            list(research["context"]["evidence_blocking_reasons"])
            == raw["evidence_blocking_reasons"]
        )
        assert research["targets"][0]["horizons"]
    else:
        assert web is None
        assert research["unavailable_reason"] == reason
    assert (selection_sha256(selected), _PUBLISHED.read_bytes()) == before


def test_retained_schema_2_is_withheld_even_for_matching_historical_identity() -> None:
    raw = safe_load(_RETAINED.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    identity = (str(raw["screening_rules_hash"]), str(raw["er_model_version"]))

    context = load_er_level_calibration_context(
        _RETAINED,
        expected_method_identity=identity,
        today=date.fromisoformat(str(raw["generated_at"])[:10]),
    )

    assert context is None


def test_published_context_reaches_web_with_formal_evidence_and_basis_coverage() -> None:
    raw = safe_load(_PUBLISHED.read_text(encoding="utf-8"))
    context = load_er_level_calibration_context(
        _PUBLISHED,
        expected_method_identity=(raw["screening_rules_hash"], raw["er_model_version"]),
        today=date.fromisoformat(str(raw["generated_at"])[:10]),
    )
    assert context is not None
    assert context.evidence_complete is raw["evidence_complete"]
    assert list(context.evidence_blocking_reasons) == raw["evidence_blocking_reasons"]
    for horizon, stored in zip(context.horizons, raw["horizons"], strict=True):
        for band, stored_band in zip(horizon.bands, stored["bands"], strict=True):
            for basis, stored_basis in zip(band.bases, stored_band["bases"], strict=True):
                assert basis.coverage.model_dump() == stored_basis["coverage"]


def test_schema_4_context_reaches_web_for_matching_identity(tmp_path) -> None:
    raw = context_artifact()
    path = tmp_path / "fixed-context.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    context = load_er_level_calibration_context(
        path,
        expected_method_identity=(raw["screening_rules_hash"], raw["er_model_version"]),
        today=date.fromisoformat(raw["generated_at"][:10]),
    )
    assert context is not None
    assert context.schema_version == 4
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
