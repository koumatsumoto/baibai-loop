"""銘柄評価・独立検証の機械的な整合確認と原価格の成立条件を読むCLI。"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from decimal import DecimalException
from pathlib import Path

import yaml

from baibai_engine.investment_judgment.contracts import (
    AssessmentError,
)
from baibai_engine.investment_judgment.independent_review.contracts import (
    load_assessment_review,
)
from baibai_engine.investment_judgment.stock_assessment.document import (
    UnpublishedAssessment,
    assessment_valuation_context,
    evaluate_assessment,
    evaluation_to_payload,
    load_assessment,
)
from baibai_engine.primitives.time import JST


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="baibai-engine investment-judgment stock-assessment-evaluate",
        description="Check Stock Assessment and Independent Review consistency without writes.",
    )
    parser.add_argument("assessment", type=Path)
    parser.add_argument("--review", type=Path)
    return parser


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    """Evaluate a Stock Assessment and print its domain evaluation.

    ``now`` fixes the instant evidence and review timestamps are checked against, so a
    caller reproducing a dated situation gets the same verdict whenever it runs.
    """
    args = build_parser().parse_args(argv)
    try:
        assessment = load_assessment(args.assessment)
        review_path = args.review
        # Evaluate authored content before an independent review file is available.
        # Publication still requires that review; a missing path is not an approval.
        review = (
            load_assessment_review(review_path)
            if review_path is not None and review_path.is_file()
            else None
        )
        # A draft file: there is no published record to bind to yet.
        evaluation = evaluate_assessment(
            assessment,
            review=review,
            now=now or datetime.now(JST),
            identity=UnpublishedAssessment.DRAFT,
        )
        payload = evaluation_to_payload(evaluation)
    except AssessmentError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    # Diagnostics cannot change publication readiness, including review_required.
    try:
        payload["valuation_context"] = assessment_valuation_context(assessment, evaluation)
    except (DecimalException, OverflowError, ValueError) as error:
        payload["valuation_context"] = None
        print(f"valuation_context unavailable: {error}", file=sys.stderr)
    yaml.safe_dump(
        payload,
        sys.stdout,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    return 0 if evaluation.assessment_readiness == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
