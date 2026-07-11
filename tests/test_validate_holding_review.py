from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from baibai_loop.position.holding_review import holding_review_json_schema
from baibai_loop.validation.holding_review import (
    discover_holding_review_files,
    validate_holding_review_file,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "records" / "_schemas" / "holding-review.json"
FIXTURES = Path(__file__).parent / "fixtures" / "holding-review"


def test_committed_schema_matches_model() -> None:
    assert json.loads(SCHEMA.read_text(encoding="utf-8")) == holding_review_json_schema()


def test_legacy_arithmetic_fixtures_are_rejected_without_current_sources() -> None:
    for name in (
        "thesis-break.yaml",
        "fair-value-hold.yaml",
        "underwater-hold.yaml",
        "replacement-superior.yaml",
        "tax-unknown-hold.yaml",
    ):
        findings = validate_holding_review_file(FIXTURES / name)
        assert any(f.code == "holding-review.invalid" for f in findings), name


def test_six_axes_fails_schema(tmp_path: Path) -> None:
    raw: dict[str, Any] = yaml.safe_load(
        (FIXTURES / "underwater-hold.yaml").read_text(encoding="utf-8")
    )
    raw["thesis_health"]["permanent_loss_axes"].pop()  # 6 axes
    draft = tmp_path / "short-review.yaml"
    draft.write_text(yaml.safe_dump(raw), encoding="utf-8")
    findings = validate_holding_review_file(draft)
    assert any(f.severity == "error" for f in findings)


def test_action_mismatch_is_error(tmp_path: Path) -> None:
    raw: dict[str, Any] = yaml.safe_load(
        (FIXTURES / "underwater-hold.yaml").read_text(encoding="utf-8")
    )
    raw["action"] = "exit"
    draft = tmp_path / "mismatch-review.yaml"
    draft.write_text(yaml.safe_dump(raw), encoding="utf-8")
    findings = validate_holding_review_file(draft)
    assert any(f.code == "holding-review.invalid" for f in findings)


def test_discover_returns_review_yaml(tmp_path: Path) -> None:
    (tmp_path / "2026" / "07").mkdir(parents=True)
    review = tmp_path / "2026" / "07" / "2026-07-15-9715-review.yaml"
    review.write_text("schema_version: 2\n", encoding="utf-8")
    (tmp_path / "2026" / "07" / "2026-07-15-9715.md").write_text("---\n", encoding="utf-8")
    found = discover_holding_review_files(tmp_path)
    assert found == [review]
