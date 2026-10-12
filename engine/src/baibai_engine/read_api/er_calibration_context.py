"""Web・外部照会からValuationの保存済み較正文脈を読む入口。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from baibai_engine.valuation.calibration import query


def load_er_calibration_context(
    path: Path,
    *,
    expected_rules_hash: str,
    expected_er_model_version: str,
    as_of: date,
) -> query.ErCalibrationContextLoad:
    return query.load_er_calibration_context(
        path,
        expected_rules_hash=expected_rules_hash,
        expected_er_model_version=expected_er_model_version,
        as_of=as_of,
    )


def stored_er_calibration_context(
    path: Path,
    *,
    expected_rules_hash: str,
    expected_er_model_version: str,
    as_of: date,
) -> tuple[dict[str, object], query.ErCalibrationUnavailableReason | None]:
    return query.stored_er_calibration_context(
        path,
        expected_rules_hash=expected_rules_hash,
        expected_er_model_version=expected_er_model_version,
        as_of=as_of,
    )
