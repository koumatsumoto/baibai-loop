from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from tools.quality.drift import check_er_calibration_context

from baibai_engine.foundation.er_calibration_context import ErCalibrationContextArtifact
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.read_api.er_calibration_context import load_er_calibration_context

_ROOT = Path(__file__).resolve().parents[2]
_PUBLISHED = _ROOT / "reports/published/er-level-calibration-latest.yaml"


def _artifact() -> dict[str, Any]:
    raw = safe_load(_PUBLISHED.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _load(path: Path, payload: dict[str, Any], *, as_of: date = date(2026, 9, 1)):
    return load_er_calibration_context(
        path,
        expected_rules_hash=str(payload["screening_rules_hash"]),
        expected_er_model_version=str(payload["er_model_version"]),
        as_of=as_of,
    )


def test_current_published_artifact_is_accepted_without_regeneration() -> None:
    artifact = ErCalibrationContextArtifact.model_validate(_artifact())

    assert artifact.schema_version == 2
    assert [horizon.horizon for horizon in artifact.horizons] == ["3y", "5y"]


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
    future["generated_at"] = "2026-09-02T00:00:00+09:00"
    future["valid_through"] = "2026-10-17"
    path = tmp_path / "future.yaml"
    _write(path, future)
    assert _load(path, future).unavailable_reason == "invalid_artifact"

    expired = deepcopy(_artifact())
    expired["generated_at"] = "2026-07-01T00:00:00+09:00"
    expired["valid_through"] = "2026-08-15"
    _write(path, expired)
    assert _load(path, expired).unavailable_reason == "expired"


def test_reader_preserves_method_identity_reasons(tmp_path: Path) -> None:
    payload = _artifact()
    path = tmp_path / "context.yaml"
    _write(path, payload)

    rules = load_er_calibration_context(
        path,
        expected_rules_hash="different",
        expected_er_model_version=str(payload["er_model_version"]),
        as_of=date(2026, 9, 1),
    )
    model = load_er_calibration_context(
        path,
        expected_rules_hash=str(payload["screening_rules_hash"]),
        expected_er_model_version="different",
        as_of=date(2026, 9, 1),
    )

    assert rules.unavailable_reason == "rules_identity_mismatch"
    assert model.unavailable_reason == "er_model_identity_mismatch"


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
