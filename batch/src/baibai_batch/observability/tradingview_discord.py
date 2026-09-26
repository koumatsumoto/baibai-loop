"""Dependency-free Discord adapter for the TradingView snapshot workflow."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable

from .discord import (
    DEFAULT_TIMEOUT_SECONDS,
    OUTCOME_CANCELLED,
    OUTCOME_DEGRADED,
    OUTCOME_FAILED,
    OUTCOME_LABELS,
    OUTCOME_OK,
    WEBHOOK_ENV_VAR,
    _urllib_transport,
    deliver,
    run_url,
    sanitize_one_line,
)

STEPS = (
    "smoke",
    "target",
    "setup",
    "setup-uv",
    "sync",
    "duckdb-httpfs",
    "pull",
    "hydrate",
    "preflight",
    "master",
    "tradingview",
    "publish-lake",
)
FAILED_STEPS = tuple(step for step in STEPS if step != "tradingview")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradingview_discord")
    parser.add_argument("--asof", default="")
    parser.add_argument("--eligible", choices=("true", "false"), default="false")
    parser.add_argument(
        "--snapshot-status",
        choices=(
            "saved",
            "already_saved",
            "non_trading_day",
            "skipped_historical_asof",
            "failure",
            "unknown",
        ),
        default="unknown",
    )
    parser.add_argument(
        "--preflight-status",
        choices=("needs_fetch", "already_saved", "non_trading_day", "unknown"),
        default="unknown",
    )
    for step in STEPS:
        parser.add_argument(f"--{step}-outcome", default="skipped")
    parser.add_argument("--cancelled", choices=("true", "false"), default="false")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    transport: Callable[[str, bytes, float], int] = _urllib_transport,
) -> int:
    args = build_parser().parse_args(argv)
    outcomes = {step: getattr(args, step.replace("-", "_") + "_outcome") for step in STEPS}
    if args.cancelled == "true":
        label, detail = OUTCOME_CANCELLED, ""
    elif failed := next(
        (step for step in FAILED_STEPS if outcomes[step] in {"failure", "cancelled"}), ""
    ):
        label, detail = OUTCOME_FAILED, f" — failed step: {failed}"
    elif outcomes["tradingview"] in {"failure", "cancelled"}:
        label, detail = OUTCOME_DEGRADED, " — failed step: tradingview"
    elif args.snapshot_status == "saved" and outcomes["publish-lake"] == "success":
        label, detail = OUTCOME_OK, " — TradingView snapshot saved"
    elif (
        (args.eligible == "false" and outcomes["target"] == "success")
        or args.preflight_status in {"already_saved", "non_trading_day"}
        or args.snapshot_status == "skipped_historical_asof"
    ):
        return 0
    else:
        label, detail = OUTCOME_FAILED, " — failed step: workflow"
    message = (
        f"{OUTCOME_LABELS[label]} as-of {sanitize_one_line(args.asof)}{detail}"
        f"\nrun: {run_url(os.environ)}"
    )
    print(message, flush=True)
    failure = deliver(
        os.environ.get(WEBHOOK_ENV_VAR, ""),
        message,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        transport=transport,
    )
    if failure is not None:
        print(f"error: discord notification failed: {failure}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
