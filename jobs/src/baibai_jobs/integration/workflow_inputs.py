"""Validate privileged workflow dispatch inputs before credentials are exposed."""

from __future__ import annotations

import argparse
import sys
from datetime import date


class WorkflowInputError(ValueError):
    """A dispatch input does not satisfy the workflow contract."""


def _validate_optional_date(value: str, *, label: str) -> None:
    if not value:
        return
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise WorkflowInputError(f"{label} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise WorkflowInputError(f"{label} must use YYYY-MM-DD")


def validate_daily_input(
    *,
    asof: str,
    preserve_existing_generations: str = "",
    edinet_metrics_acceptance: str = "",
) -> None:
    """Validate the manual daily-batch date and explicit retention choice."""
    _validate_optional_date(asof, label="asof")
    if preserve_existing_generations not in ("", "true", "false"):
        raise WorkflowInputError("preserve_existing_generations must be empty, true, or false")
    if edinet_metrics_acceptance:
        from baibai_jobs.integration.edinet_metric_acceptance import decode_acceptance

        try:
            acceptance = decode_acceptance(edinet_metrics_acceptance)
        except (ValueError, TypeError) as exc:
            raise WorkflowInputError("EDINET metric acceptance input is invalid") from exc
        if acceptance["as_of"] != asof or preserve_existing_generations != "true":
            raise WorkflowInputError(
                "EDINET acceptance requires the explicit date and generation retention"
            )


def validate_watchdog_input(*, check_date: str) -> None:
    """Validate the optional date whose scheduled watchdog window to evaluate."""
    _validate_optional_date(check_date, label="check_date")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    daily = subparsers.add_parser("daily")
    daily.add_argument("--asof", default="")
    daily.add_argument("--preserve-existing-generations", default="")
    daily.add_argument("--edinet-metrics-acceptance", default="")

    watchdog = subparsers.add_parser("watchdog")
    watchdog.add_argument("--check-date", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "daily":
            validate_daily_input(
                asof=args.asof,
                preserve_existing_generations=args.preserve_existing_generations,
                edinet_metrics_acceptance=args.edinet_metrics_acceptance,
            )
        else:
            validate_watchdog_input(check_date=args.check_date)
    except WorkflowInputError as exc:
        print(f"invalid workflow input: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
