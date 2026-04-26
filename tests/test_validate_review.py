from __future__ import annotations

from pathlib import Path

from baibai_loop.validate.review import discover_review_files, validate_review_file


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
    assert "review.missing-field" in {finding.code for finding in validate_review_file(path)}


def test_review_unknown_classification_is_flagged(tmp_path: Path) -> None:
    path = tmp_path / "review.md"
    path.write_text(_review_text("unknown"))
    assert "review.invalid-classification" in {
        finding.code for finding in validate_review_file(path)
    }


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
