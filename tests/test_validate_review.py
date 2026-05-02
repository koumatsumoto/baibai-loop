from __future__ import annotations

from pathlib import Path

from baibai_loop.validate.review import (
    KNOWN_CLASSIFICATIONS,
    discover_review_files,
    validate_review_file,
)


def _review_text(classification: str = "success") -> str:
    return f"""---
trade_ref: trades/2026/04/example.md
classification: {classification}
verified_at: "2026-04-30"
---

# Review

## Outcome
## Hypothesis check
## Process check
## Lessons
## Next actions
"""


def test_review_missing_required_front_matter_is_flagged(tmp_path: Path) -> None:
    path = tmp_path / "review.md"
    path.write_text(_review_text().replace("trade_ref: trades/2026/04/example.md\n", ""))
    assert "review.required" in {finding.code for finding in validate_review_file(path)}


def test_review_unknown_classification_is_flagged(tmp_path: Path) -> None:
    path = tmp_path / "review.md"
    path.write_text(_review_text("unknown"))
    assert "review.enum" in {finding.code for finding in validate_review_file(path)}


def test_review_invalid_yaml_is_returned_as_finding(tmp_path: Path) -> None:
    path = tmp_path / "review.md"
    path.write_text('---\nclassification: "unterminated\n---\n# Review\n')
    assert "review.invalid-yaml" in {finding.code for finding in validate_review_file(path)}


def test_empty_reviews_directory_discovers_no_files(tmp_path: Path) -> None:
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    assert discover_review_files(reviews) == []


def test_review_template_is_skipped(tmp_path: Path) -> None:
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    template = reviews / "template.md"
    template.write_text(_review_text())
    assert discover_review_files(reviews) == []


def test_monthly_retro_file_uses_retro_schema(tmp_path: Path) -> None:
    path = tmp_path / "retro-202604.md"
    path.write_text(
        """---
retro_month: "2026-04"
total_trades: 0
open_trades: 0
closed_trades: 0
skipped_candidates: 0
wins: 0
losses: 0
pnl_pct_sum: 0.0
failure_class_counts:
  材料誤読: 0
success_class_counts:
  仮説的中: 0
playbook_revision_decision: "v1 据え置き"
next_cycle_changes:
  - "実 trade がないため、次周回も採用後の執行記録を優先する"
price_missing_counts:
  plus_15bd: 0
  plus_30bd: 0
---

# Retro: 2026-04 月次振り返り

## Trade 集計
## 失敗分類の集計
## 成功分類の集計
## Skipped trade log の分析
## Macro gate 判定精度
## Playbook 改訂判断
## 次周回の運用変更点
""",
        encoding="utf-8",
    )
    assert validate_review_file(path) == []


def test_known_classifications_are_derived_from_schema() -> None:
    # schema の enum と KNOWN_CLASSIFICATIONS が drift しないことを担保する。
    assert KNOWN_CLASSIFICATIONS == ("success", "failure", "invalidated", "inconclusive")
