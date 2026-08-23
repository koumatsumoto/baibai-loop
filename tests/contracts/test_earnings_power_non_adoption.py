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
    assert median(row["alt_only_count"] for row in geometry) == 18
    assert median(row["earnings_sector_concentration"] for row in geometry) == pytest.approx(0.25)
    assert verdict_inputs["sensitivity_sign_split"] is True
    assert verdict_inputs["total_return_coverage_below_floor"] is True
