from __future__ import annotations

from pathlib import Path

from baibai_loop.validate.review import (
    KNOWN_CLASSIFICATIONS,
    discover_review_files,
    validate_review_file,
)


def _review_text(classification: str = "success") -> str:
    return f"""---
decision_event_id: decision-1
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


def _write_referenced_markdown(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nrecord_id: referenced\n---\n# Referenced\n", encoding="utf-8")


def test_review_missing_required_front_matter_is_flagged(tmp_path: Path) -> None:
    path = tmp_path / "review.md"
    path.write_text(_review_text().replace("classification: success\n", ""))
    assert "review.required" in {finding.code for finding in validate_review_file(path)}


def test_review_unknown_classification_is_flagged(tmp_path: Path) -> None:
    path = tmp_path / "review.md"
    path.write_text(_review_text("unknown"))
    assert "review.enum" in {finding.code for finding in validate_review_file(path)}


def test_review_rejects_missing_research_ref_file(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    path = tmp_path / "records/07-reviews/2026/05/review.md"
    path.parent.mkdir(parents=True)
    trade = tmp_path / "records/06-trades/2026/04/example.md"
    _write_referenced_markdown(trade)
    path.write_text(
        _review_text().replace(
            "trade_ref: records/06-trades/2026/04/example.md\n",
            "research_ref: records/05-research/2026/05/missing.md\n"
            "trade_ref: records/06-trades/2026/04/example.md\n",
        ),
        encoding="utf-8",
    )

    assert "review.repository-ref" in {finding.code for finding in validate_review_file(path)}


def test_review_rejects_trade_ref_wrong_prefix(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    path = tmp_path / "records/07-reviews/2026/05/review.md"
    path.parent.mkdir(parents=True)
    wrong_trade = tmp_path / "records/05-research/2026/05/research.md"
    _write_referenced_markdown(wrong_trade)
    path.write_text(
        _review_text().replace(
            "records/06-trades/2026/04/example.md",
            "records/05-research/2026/05/research.md",
        ),
        encoding="utf-8",
    )

    assert "review.repository-ref" in {finding.code for finding in validate_review_file(path)}


def test_review_accepts_existing_research_and_trade_refs(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    path = tmp_path / "records/07-reviews/2026/05/review.md"
    path.parent.mkdir(parents=True)
    research = tmp_path / "records/05-research/2026/05/research.md"
    trade = tmp_path / "records/06-trades/2026/04/example.md"
    _write_referenced_markdown(research)
    _write_referenced_markdown(trade)
    path.write_text(
        _review_text().replace(
            "trade_ref: records/06-trades/2026/04/example.md\n",
            "research_ref: records/05-research/2026/05/research.md\n"
            "trade_ref: records/06-trades/2026/04/example.md\n",
        ),
        encoding="utf-8",
    )

    assert validate_review_file(path) == []


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
    reviews = tmp_path / "records/07-reviews/playbook-attribution"
    reviews.mkdir(parents=True)
    scan = reviews / "2026-05.yaml"
    scan.write_text("items: []\n", encoding="utf-8")

    assert discover_review_files(tmp_path / "records/07-reviews") == [scan]


def test_review_scan_rejects_invalid_market_data_ref(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    scan = tmp_path / "records/07-reviews/playbook-attribution/2026-05.yaml"
    scan.parent.mkdir(parents=True)
    scan.write_text(
        "scan_id: scan-1\nmarket_data_ref:\n  ref_path: /tmp/market-data.yaml\nitems: []\n",
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
## Missed opportunity tracking の分析
## Macro context fit 判定精度
## Playbook 改訂判断
## 次周回の運用変更点
""",
        encoding="utf-8",
    )
    assert validate_review_file(path) == []


def test_known_classifications_are_derived_from_schema() -> None:
    # schema の enum と KNOWN_CLASSIFICATIONS が drift しないことを担保する。
    assert KNOWN_CLASSIFICATIONS == ("success", "failure", "invalidated", "inconclusive")
