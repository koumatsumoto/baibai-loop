"""Validate privileged workflow dispatch dates before credentials are exposed."""

from __future__ import annotations

import argparse
import sys
from datetime import date


class WorkflowInputError(ValueError):
    """A dispatch input does not satisfy the workflow date contract."""


def _validate_optional_date(value: str, *, label: str) -> None:
    if not value:
        return
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise WorkflowInputError(f"{label} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise WorkflowInputError(f"{label} must use YYYY-MM-DD")


def validate_daily_input(*, asof: str) -> None:
    """Validate the optional manual daily-batch as-of date."""
    _validate_optional_date(asof, label="asof")


def validate_watchdog_input(*, check_date: str) -> None:
    """Validate the optional date whose scheduled watchdog window to evaluate."""
    _validate_optional_date(check_date, label="check_date")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    daily = subparsers.add_parser("daily")
    daily.add_argument("--asof", default="")

    watchdog = subparsers.add_parser("watchdog")
    watchdog.add_argument("--check-date", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "daily":
            validate_daily_input(asof=args.asof)
        else:
            validate_watchdog_input(check_date=args.check_date)
    except WorkflowInputError as exc:
        print(f"invalid workflow input: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
