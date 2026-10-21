"""Valuationの検証済みER較正文脈をWebへ渡す。"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from baibai_engine.read_api.er_calibration_context import (
    ErCalibrationContextArtifact,
    load_er_calibration_context,
)


def load_er_level_calibration_context(
    path: Path,
    *,
    expected_method_identity: tuple[str, str] | None,
    today: date | None = None,
) -> ErCalibrationContextArtifact | None:
    """Read the validated Valuation artifact for the current Web method."""

    if expected_method_identity is None:
        return None
    rules_hash, er_model_version = expected_method_identity
    loaded = load_er_calibration_context(
        path,
        expected_rules_hash=rules_hash,
        expected_er_model_version=er_model_version,
        as_of=today or datetime.now(ZoneInfo("Asia/Tokyo")).date(),
    )
    return loaded.artifact
