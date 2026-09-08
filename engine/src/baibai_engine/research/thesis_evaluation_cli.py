"""Researchの公開内容評価と原価格の成立条件を読み取り専用で見せるCLI。"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from decimal import DecimalException
from pathlib import Path

import yaml

from .thesis import (
    ThesisError,
    UnpublishedThesis,
    evaluate_thesis,
    evaluation_to_payload,
    load_thesis,
    load_thesis_review,
    thesis_valuation_context,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="baibai-engine research evaluate",
        description="Evaluate a thesis draft against the decision gate without writing anything.",
    )
    parser.add_argument("thesis", type=Path)
    parser.add_argument("--review", type=Path)
    return parser


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    """Evaluate a Thesis and print its domain evaluation.

    ``now`` fixes the instant evidence and overrides are judged against, so a
    caller reproducing a dated situation gets the same verdict whenever it runs.
    """
    args = build_parser().parse_args(argv)
    try:
        thesis = load_thesis(args.thesis)
        review_path = args.review
        # The thesis scaffold reserves a stable review ref before that file exists.
        # Thesis evaluation is useful first; promotion still requires the review.
        review = (
            load_thesis_review(review_path)
            if review_path is not None and review_path.is_file()
            else None
        )
        # A draft file: there is no published record to bind to yet.
        evaluation = evaluate_thesis(
            thesis, review=review, now=now, identity=UnpublishedThesis.DRAFT
        )
        payload = evaluation_to_payload(evaluation)
    except ThesisError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    # Diagnostics cannot change publication readiness, including review_required.
    try:
        payload["valuation_context"] = thesis_valuation_context(thesis, evaluation)
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
    return 0 if evaluation.decision_readiness == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
