from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from tests.helpers.er_calibration_context import context_artifact
from tools.quality.drift import check_er_calibration_context

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.valuation.calibration.contracts import ErCalibrationContextArtifact
from baibai_engine.valuation.calibration.query import load_er_calibration_context

_ROOT = Path(__file__).resolve().parents[2]
_PUBLISHED = _ROOT / "reports/published/er-level-calibration-latest.yaml"


def _artifact() -> dict[str, Any]:
    return context_artifact(rules_hash="7f934c6cdf199a33")


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _load(path: Path, payload: dict[str, Any], *, as_of: date | None = None):
    return load_er_calibration_context(
        path,
        expected_rules_hash=str(payload["screening_rules_hash"]),
        expected_er_model_version=str(payload["er_model_version"]),
        as_of=as_of or date.fromisoformat(str(payload["generated_at"])[:10]),
    )


def test_retained_schema_2_is_not_reinterpreted_even_for_its_original_method() -> None:
    raw = safe_load(_PUBLISHED.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    assert raw["schema_version"] == 2
    original = _PUBLISHED.read_bytes()
    assert _load(_PUBLISHED, raw).unavailable_reason == "invalid_artifact"
    assert not check_er_calibration_context.check()
    assert _PUBLISHED.read_bytes() == original


def test_schema_3_fixed_band_contract_is_accepted() -> None:
    artifact = ErCalibrationContextArtifact.model_validate(_artifact())
    assert artifact.schema_version == 3
    assert [horizon.horizon for horizon in artifact.horizons] == ["3y", "5y"]


def test_retained_schema_2_bytes_cannot_be_relabelled_or_modified(tmp_path, monkeypatch) -> None:
    raw = safe_load(_PUBLISHED.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    raw["horizons"][0]["bands"][0]["median_n"] += 1
    path = tmp_path / "changed-schema-2.yaml"
    _write(path, raw)
    monkeypatch.setattr(check_er_calibration_context, "_CONTEXT", path)
    assert (
        "retained schema 2 E[r] context bytes differ from the original"
        in check_er_calibration_context.check()
    )
    assert _load(path, raw).artifact is None


@pytest.mark.parametrize("location", ["root", "band", "stats"])
def test_unknown_fields_are_rejected_at_every_owned_level(location: str) -> None:
    payload = deepcopy(_artifact())
    if location == "root":
        payload["unknown"] = "value"
    elif location == "band":
        payload["horizons"][0]["bands"][0]["unknown"] = "value"
    else:
        payload["horizons"][0]["bands"][0]["bases"][0]["ticker_equal"]["unknown"] = "value"

    with pytest.raises(ValidationError):
        ErCalibrationContextArtifact.model_validate(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["horizons"][0]["bands"].reverse(),
        lambda payload: payload["horizons"][0]["bands"][1].update(lower_er_annual=-999.0),
        lambda payload: payload["horizons"][0]["bands"][0]["bases"].reverse(),
        lambda payload: payload.update(valid_through="2099-01-01"),
    ],
    ids=["band-order", "band-gap", "basis-order", "ttl"],
)
def test_invalid_structural_contract_is_rejected(mutate: Any) -> None:
    payload = deepcopy(_artifact())
    mutate(payload)

    with pytest.raises(ValidationError):
        ErCalibrationContextArtifact.model_validate(payload)


def test_reader_preserves_future_and_expired_point_in_time_reasons(tmp_path: Path) -> None:
    future = deepcopy(_artifact())
    future["generated_at"] = "2026-09-04T00:00:00+09:00"
    future["valid_through"] = "2026-10-19"
    path = tmp_path / "future.yaml"
    _write(path, future)
    assert _load(path, future, as_of=date(2026, 9, 3)).unavailable_reason == "invalid_artifact"

    expired = deepcopy(_artifact())
    expired["generated_at"] = "2026-07-01T00:00:00+09:00"
    expired["valid_through"] = "2026-08-15"
    _write(path, expired)
    assert _load(path, expired, as_of=date(2026, 9, 3)).unavailable_reason == "expired"


def test_reader_preserves_method_identity_reasons(tmp_path: Path) -> None:
    payload = _artifact()
    path = tmp_path / "context.yaml"
    _write(path, payload)

    rules = load_er_calibration_context(
        path,
        expected_rules_hash="different",
        expected_er_model_version=str(payload["er_model_version"]),
        as_of=date.fromisoformat(str(payload["generated_at"])[:10]),
    )
    model = load_er_calibration_context(
        path,
        expected_rules_hash=str(payload["screening_rules_hash"]),
        expected_er_model_version="different",
        as_of=date.fromisoformat(str(payload["generated_at"])[:10]),
    )

    assert rules.unavailable_reason == "rules_identity_mismatch"
    assert model.unavailable_reason == "er_model_identity_mismatch"


def test_pre_one_bar_scope_context_keeps_provenance_but_is_not_current(
    tmp_path: Path, monkeypatch
) -> None:
    from baibai_engine.screening.rule_config import load_screening_rules
    from baibai_engine.screening.rules_identity import production_rules_contract_hash

    rules_json = load_screening_rules().model_dump_json()
    old_hash = production_rules_contract_hash(rules_json, universe_scope_revision="")
    current_hash = production_rules_contract_hash(rules_json)
    assert old_hash != current_hash
    payload = context_artifact(rules_hash=old_hash)
    path = tmp_path / "old-scope-context.yaml"
    _write(path, payload)
    monkeypatch.setattr(check_er_calibration_context, "_CONTEXT", path)
    assert not check_er_calibration_context.check()
    assert (
        load_er_calibration_context(
            path,
            expected_rules_hash=current_hash,
            expected_er_model_version=str(payload["er_model_version"]),
            as_of=date.fromisoformat(str(payload["generated_at"])[:10]),
        ).unavailable_reason
        == "rules_identity_mismatch"
    )


@pytest.mark.parametrize("scope_revision", ["", "valid-price-one-bar-v2"])
def test_pre_dividend_revision_context_is_retained_but_refused_for_current_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope_revision: str
) -> None:
    from baibai_engine.screening.rule_config import load_screening_rules
    from baibai_engine.screening.rules_identity import production_rules_contract_hash
    from baibai_engine.valuation.metrics import financial_snapshot

    rules_json = load_screening_rules().model_dump_json()
    with monkeypatch.context() as previous:
        previous.setattr(
            financial_snapshot, "VALUATION_CALCULATION_REVISION", "same-profit-basis-yoy-v25"
        )
        old_hash = production_rules_contract_hash(
            rules_json, universe_scope_revision=scope_revision
        )
    current_hash = production_rules_contract_hash(rules_json)
    assert old_hash != current_hash
    payload = context_artifact(rules_hash=old_hash)
    path = tmp_path / "old-dividend-context.yaml"
    _write(path, payload)
    original = path.read_bytes()
    monkeypatch.setattr(check_er_calibration_context, "_CONTEXT", path)

    assert not check_er_calibration_context.check()
    result = load_er_calibration_context(
        path,
        expected_rules_hash=current_hash,
        expected_er_model_version="expected-return-v1",
        as_of=date(2026, 10, 13),
    )
    assert result.artifact is None
    assert result.unavailable_reason == "rules_identity_mismatch"
    assert _load(path, payload).artifact is not None
    assert path.read_bytes() == original


def test_retained_context_is_withheld_from_new_population_without_relabeling(
    tmp_path, monkeypatch
) -> None:
    import json

    from baibai_engine.screening.rule_config import load_screening_rules
    from baibai_engine.screening.rules_identity import production_rules_contract_hash

    old = load_screening_rules(_ROOT / "method/screening/rules/2026-09-23T103000+0900.yaml")
    new = old.model_copy(
        update={
            "candidate_discovery": old.candidate_discovery.model_copy(
                update={
                    "common_eligibility": old.candidate_discovery.common_eligibility.model_copy(
                        update={"min_market_cap_oku": 0.0}
                    )
                }
            )
        }
    )
    rules_dir = tmp_path / "method/screening/rules"
    rules_dir.mkdir(parents=True)
    for name, rules in (("old", old), ("new", new)):
        (rules_dir / f"{name}.yaml").write_text(
            yaml.safe_dump(json.loads(rules.model_dump_json()), sort_keys=False)
        )
    payload = context_artifact(rules_hash=production_rules_contract_hash(old.model_dump_json()))
    path = tmp_path / "context.yaml"
    _write(path, payload)
    original = path.read_bytes()
    current_hash = production_rules_contract_hash(new.model_dump_json())
    monkeypatch.setattr(check_er_calibration_context, "_ROOT", tmp_path)
    monkeypatch.setattr(check_er_calibration_context, "_CONTEXT", path)
    monkeypatch.setattr(
        check_er_calibration_context, "DEFAULT_RULES_PATH", Path("method/screening/rules/new.yaml")
    )
    assert not check_er_calibration_context.check()
    result = load_er_calibration_context(
        path,
        expected_rules_hash=current_hash,
        expected_er_model_version=str(payload["er_model_version"]),
        as_of=date.fromisoformat(str(payload["generated_at"])[:10]),
    )
    assert result.artifact is None
    assert result.unavailable_reason == "rules_identity_mismatch"
    assert _load(path, payload).artifact is not None
    assert path.read_bytes() == original


def test_drift_checker_refuses_untraceable_evidence(tmp_path: Path, monkeypatch) -> None:
    payload = _artifact()
    payload["screening_rules_hash"] = "untraceable"
    path = tmp_path / "context.yaml"
    _write(path, payload)
    monkeypatch.setattr(check_er_calibration_context, "_CONTEXT", path)
    assert check_er_calibration_context.check() == (
        "published E[r] context screening_rules_hash has no known rules revision",
    )


def test_retired_quality_knob_keeps_the_original_frozen_rules_provenance(tmp_path, monkeypatch):
    from baibai_engine.screening.rule_config import load_screening_rules

    frozen = _ROOT / "method/screening/rules/2026-09-23T103000+0900.yaml"
    original = frozen.read_bytes()
    assert "yoy_deterioration_threshold" not in load_screening_rules(frozen).quality.model_dump()
    # Original production fingerprint, captured before the unused field was removed.
    payload = context_artifact(rules_hash="7f934c6cdf199a33")
    context = tmp_path / "retained-context.yaml"
    _write(context, payload)
    monkeypatch.setattr(check_er_calibration_context, "_CONTEXT", context)

    assert not check_er_calibration_context.check()
    assert frozen.read_bytes() == original
    assert _load(context, payload).artifact is not None


@pytest.mark.parametrize("valid", [True, False], ids=["valid", "invalid"])
def test_drift_checker_and_reader_have_validity_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valid: bool
) -> None:
    payload = deepcopy(_artifact())
    if not valid:
        payload["horizons"][0]["bands"][0]["unknown"] = "value"
    path = tmp_path / "context.yaml"
    _write(path, payload)
    monkeypatch.setattr(check_er_calibration_context, "_CONTEXT", path)

    drift_valid = not check_er_calibration_context.check()
    reader_valid = _load(path, payload).artifact is not None

    assert drift_valid is valid
    assert reader_valid is valid
