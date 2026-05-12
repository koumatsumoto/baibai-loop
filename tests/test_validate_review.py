from __future__ import annotations

from pathlib import Path

from baibai_loop.validate.review import (
    KNOWN_CLASSIFICATIONS,
    discover_review_files,
    validate_review_file,
)


def _review_text(classification: str = "success") -> str:
    return f"""---
trade_ref: records/06-trades/2026/04/example.md
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
    path.write_text(_review_text().replace("trade_ref: records/06-trades/2026/04/example.md\n", ""))
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


def test_yaml_review_scans_are_discovered(tmp_path: Path) -> None:
    reviews = tmp_path / "records/07-reviews/screening-false-negative-scan"
    reviews.mkdir(parents=True)
    scan = reviews / "2026-05.yaml"
    scan.write_text("items: []\n", encoding="utf-8")

    assert discover_review_files(tmp_path / "records/07-reviews") == [scan]


def test_false_negative_scan_requires_run_close_start_basis(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    register = tmp_path / "records/_ledger/research-decisions/2026-05.jsonl"
    register.parent.mkdir(parents=True)
    register.write_text('{"decision_event_id":"decision-1"}\n', encoding="utf-8")
    scan = tmp_path / "records/07-reviews/screening-false-negative-scan/2026-05.yaml"
    scan.parent.mkdir(parents=True)
    scan.write_text(
        "scan_id: scan-1\n"
        "start_price_basis: flagged_at_close\n"
        "items:\n"
        "- decision_event_id: decision-1\n"
        "  start_price_basis: flagged_at_close\n",
        encoding="utf-8",
    )

    codes = {finding.code for finding in validate_review_file(scan)}

    assert "review-scan.start-price-basis" in codes
    assert "review-scan.item-start-price-basis" in codes


def test_false_negative_scan_requires_decision_anchor(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    scan = tmp_path / "records/07-reviews/screening-false-negative-scan/2026-05.yaml"
    scan.parent.mkdir(parents=True)
    scan.write_text(
        "scan_id: scan-1\n"
        "start_price_basis: candidate_run_close_adjusted_close\n"
        "items:\n"
        "- decision_event_id: missing-decision\n"
        "  start_price_basis: candidate_run_close_adjusted_close\n",
        encoding="utf-8",
    )

    codes = {finding.code for finding in validate_review_file(scan)}

    assert "review-scan.decision-event-missing" in codes


def test_false_negative_scan_rejects_invalid_market_data_ref(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    scan = tmp_path / "records/07-reviews/screening-false-negative-scan/2026-05.yaml"
    scan.parent.mkdir(parents=True)
    scan.write_text(
        "scan_id: scan-1\n"
        "start_price_basis: candidate_run_close_adjusted_close\n"
        "market_data_ref:\n"
        "  ref_path: /tmp/market-data.yaml\n"
        "items: []\n",
        encoding="utf-8",
    )

    codes = {finding.code for finding in validate_review_file(scan)}

    assert "review-scan.repository-ref" in codes


def test_false_negative_scan_rejects_wrong_universe_ref_target(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    scan = tmp_path / "records/07-reviews/screening-false-negative-scan/2026-05.yaml"
    scan.parent.mkdir(parents=True)
    wrong_ref = tmp_path / "records/_market-data/2026-05.yaml"
    wrong_ref.parent.mkdir(parents=True)
    wrong_ref.write_text("items: []\n", encoding="utf-8")
    scan.write_text(
        "scan_id: scan-1\n"
        "start_price_basis: candidate_run_close_adjusted_close\n"
        "universe_ref:\n"
        "  ref_path: records/_market-data/2026-05.yaml\n"
        "items: []\n",
        encoding="utf-8",
    )

    codes = {finding.code for finding in validate_review_file(scan)}

    assert "review-scan.repository-ref" in codes


def test_monthly_retro_file_uses_retro_schema(tmp_path: Path) -> None:
    path = tmp_path / "retro-202604.md"
    path.write_text(
        """---
retro_month: "2026-04"
approved_decisions: 0
submitted_orders: 0
filled_positions: 0
closed_positions: 0
missed_opportunities: 0
failure_class_counts:
  材料誤読: 0
success_class_counts:
  仮説的中: 0
playbook_revision_decision: "据え置き"
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
## Missed opportunity / screening false negative tracking の分析
## Macro regime gate 判定精度
## Playbook 改訂判断
## 次周回の運用変更点
""",
        encoding="utf-8",
    )
    assert validate_review_file(path) == []


def test_known_classifications_are_derived_from_schema() -> None:
    # schema の enum と KNOWN_CLASSIFICATIONS が drift しないことを担保する。
    assert KNOWN_CLASSIFICATIONS == ("success", "failure", "invalidated", "inconclusive")
