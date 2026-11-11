"""Validate privileged workflow dispatch dates before credentials are exposed."""

from __future__ import annotations

import argparse
import re
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


def validate_recovery_input(*, contract_sha256: str) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", contract_sha256) is None:
        raise WorkflowInputError("recovery contract must be a lowercase SHA-256 digest")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    daily = subparsers.add_parser("daily")
    daily.add_argument("--asof", default="")

    watchdog = subparsers.add_parser("watchdog")
    watchdog.add_argument("--check-date", default="")
    recovery = subparsers.add_parser("jpx-recovery")
    recovery.add_argument("--contract-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "daily":
            validate_daily_input(asof=args.asof)
        elif args.command == "watchdog":
            validate_watchdog_input(check_date=args.check_date)
        else:
            validate_recovery_input(contract_sha256=args.contract_sha256)
    except WorkflowInputError as exc:
        print(f"invalid workflow input: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
