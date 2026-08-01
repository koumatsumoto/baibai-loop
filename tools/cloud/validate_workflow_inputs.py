"""Validate privileged workflow dispatch dates before credentials are exposed."""

from __future__ import annotations

import argparse
import sys
from datetime import date


class WorkflowInputError(ValueError):
    """A dispatch input does not satisfy the workflow date contract."""


def _exact_date(value: str, *, label: str, required: bool) -> date | None:
    if not value:
        if required:
            raise WorkflowInputError(f"{label} is required")
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise WorkflowInputError(f"{label} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise WorkflowInputError(f"{label} must use YYYY-MM-DD")
    return parsed


def validate_daily_input(*, asof: str) -> None:
    """Validate the optional manual daily-batch as-of date."""
    _exact_date(asof, label="asof", required=False)


def validate_backfill_inputs(
    *,
    start: str,
    end: str,
    master_month_end_from: str,
) -> None:
    """Validate the history window and optional master-snapshot lower bound."""
    start_date = _exact_date(start, label="start", required=True)
    end_date = _exact_date(end, label="end", required=True)
    master_start = _exact_date(
        master_month_end_from,
        label="master_month_end_from",
        required=False,
    )
    if start_date is None or end_date is None:
        raise RuntimeError("required date validation invariant failed")
    if start_date > end_date:
        raise WorkflowInputError("start must not be after end")
    if master_start is not None and master_start > end_date:
        raise WorkflowInputError("master_month_end_from must not be after end")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    daily = subparsers.add_parser("daily")
    daily.add_argument("--asof", default="")

    backfill = subparsers.add_parser("backfill")
    backfill.add_argument("--start", required=True)
    backfill.add_argument("--end", required=True)
    backfill.add_argument("--master-month-end-from", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "daily":
            validate_daily_input(asof=args.asof)
        else:
            validate_backfill_inputs(
                start=args.start,
                end=args.end,
                master_month_end_from=args.master_month_end_from,
            )
    except WorkflowInputError as exc:
        print(f"invalid workflow input: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
