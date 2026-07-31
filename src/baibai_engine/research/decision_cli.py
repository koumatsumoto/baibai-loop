"""Read-only CLI for deterministic thesis evaluation."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml

from .thesis import (
    ThesisError,
    evaluate_thesis,
    load_independent_review,
    load_thesis,
    result_to_payload,
)


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    """Evaluate a thesis file and print the domain result.

    ``now`` fixes the instant evidence and overrides are judged against, so a
    caller reproducing a dated situation gets the same verdict whenever it runs.
    """
    parser = argparse.ArgumentParser(prog="baibai-engine research evaluate")
    parser.add_argument("thesis", type=Path)
    args = parser.parse_args(argv)
    try:
        document = load_thesis(args.thesis)
        review_path = _review_path(args.thesis, document.independent_review_ref)
        review = load_independent_review(review_path) if review_path is not None else None
        result = evaluate_thesis(document, review=review, now=now)
        payload = result_to_payload(result)
    except ThesisError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    yaml.safe_dump(
        payload,
        sys.stdout,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    return 0 if result.decision_readiness == "ready" else 2


def _review_path(thesis_path: Path, review_ref: str | None) -> Path | None:
    if review_ref is None:
        return None
    root = thesis_path.resolve().parent
    resolved = (root / review_ref).resolve()
    if not resolved.is_relative_to(root):
        raise ThesisError("independent_review_ref must stay beside the thesis")
    return resolved


if __name__ == "__main__":
    raise SystemExit(main())
