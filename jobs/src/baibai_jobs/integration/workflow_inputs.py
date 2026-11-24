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
    edinet_metrics_acceptance: str = "",
    article_semantic_recovery: str = "",
) -> None:
    """Validate the manual daily-batch date and exact dates for explicit checks."""
    _validate_optional_date(asof, label="asof")
    if article_semantic_recovery:
        from baibai_jobs.integration.article_semantic_recovery import decode_recovery

        try:
            recovery = decode_recovery(article_semantic_recovery)
        except (ValueError, TypeError) as exc:
            raise WorkflowInputError("article semantic recovery input is invalid") from exc
        if recovery["as_of"] != asof:
            raise WorkflowInputError("article recovery requires the matching explicit date")
    if edinet_metrics_acceptance:
        from baibai_jobs.integration.edinet_metric_acceptance import decode_acceptance

        try:
            acceptance = decode_acceptance(edinet_metrics_acceptance)
        except (ValueError, TypeError) as exc:
            raise WorkflowInputError("EDINET metric acceptance input is invalid") from exc
        if acceptance["as_of"] != asof:
            raise WorkflowInputError("EDINET acceptance requires the matching explicit date")


def validate_watchdog_input(*, check_date: str) -> None:
    """Validate the optional date whose scheduled watchdog window to evaluate."""
    _validate_optional_date(check_date, label="check_date")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    daily = subparsers.add_parser("daily")
    daily.add_argument("--asof", default="")
    daily.add_argument("--edinet-metrics-acceptance", default="")
    daily.add_argument("--article-semantic-recovery", default="")

    watchdog = subparsers.add_parser("watchdog")
    watchdog.add_argument("--check-date", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "daily":
            validate_daily_input(
                asof=args.asof,
                edinet_metrics_acceptance=args.edinet_metrics_acceptance,
                article_semantic_recovery=args.article_semantic_recovery,
            )
        else:
            validate_watchdog_input(check_date=args.check_date)
    except WorkflowInputError as exc:
        print(f"invalid workflow input: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
