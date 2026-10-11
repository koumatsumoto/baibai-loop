"""Dependency-free terminal run notification using only job results and counts."""

from __future__ import annotations

import argparse
import os
import sys

from .discord import (
    DEFAULT_TIMEOUT_SECONDS,
    OUTCOME_CANCELLED,
    OUTCOME_DEGRADED,
    OUTCOME_FAILED,
    OUTCOME_LABELS,
    OUTCOME_OK,
    OUTCOME_SKIPPED,
    WEBHOOK_ENV_VAR,
    Transport,
    _urllib_transport,
    deliver,
    executor_label,
    run_url,
    sanitize_one_line,
)

JOB_RESULTS = ("success", "failure", "cancelled", "skipped")
COUNTERS = (
    "expected_pairs",
    "typed_pairs",
    "known_failure_pairs",
    "normal_null_pairs",
    "rows_added",
)


def _counter(value: str) -> int | None:
    if value in {"", "unknown"}:
        return None
    if not value.isascii() or not value.isdigit() or len(value) > 12:
        raise argparse.ArgumentTypeError("counter must be nonnegative digits or unknown")
    return int(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="retail_discord")
    for job in ("plan", "acquire", "publish"):
        parser.add_argument(f"--{job}-result", choices=JOB_RESULTS, default="skipped")
    for counter in COUNTERS:
        parser.add_argument(f"--{counter.replace('_', '-')}", type=_counter, default=None)
    parser.add_argument("--cancelled", choices=("true", "false"), default="false")
    return parser


def render_message(args: argparse.Namespace) -> str:
    if args.cancelled == "true" or "cancelled" in (
        args.plan_result,
        args.acquire_result,
        args.publish_result,
    ):
        outcome = OUTCOME_CANCELLED
        detail = (
            "取消・公開確認済み"
            if args.publish_result == "success"
            else "取消・この run の公開確認未完了"
        )
    elif args.publish_result == "success":
        complete_counts = all(getattr(args, name) is not None for name in COUNTERS)
        consistent_counts = complete_counts and (
            args.expected_pairs > 0
            and args.typed_pairs + args.known_failure_pairs == args.expected_pairs
            and args.normal_null_pairs <= args.typed_pairs
            and args.rows_added <= args.typed_pairs
        )
        if not consistent_counts:
            outcome, detail = OUTCOME_DEGRADED, "公開確認済み・品質件数不明"
        elif args.known_failure_pairs > 0:
            outcome, detail = OUTCOME_DEGRADED, "公開確認済み・既知の取得/parse 失敗あり"
        else:
            outcome, detail = OUTCOME_OK, "試行完了・公開確認済み"
    elif args.plan_result == "failure":
        outcome, detail = OUTCOME_FAILED, "plan 失敗・この run の公開なし"
    elif args.acquire_result == "failure":
        outcome, detail = OUTCOME_FAILED, "取得停止/未完了・この run の公開なし・停止原因未確認"
    elif args.publish_result == "failure":
        outcome, detail = OUTCOME_FAILED, "publish 失敗・公開確認未完了"
    else:
        outcome, detail = OUTCOME_SKIPPED, "この run の公開なし"

    # A matrix failure supplies no trustworthy aggregate; never fill it from R2.
    def count(name: str) -> str:
        value = getattr(args, name) if args.publish_result == "success" else None
        return str(value) if value is not None else "不明"

    return (
        f"{OUTCOME_LABELS[outcome]} retail dynamics — {detail}"
        f"\n固定 pair={count('expected_pairs')}; typed pair={count('typed_pairs')}; "
        f"既知失敗 pair={count('known_failure_pairs')}; "
        f"全評価値 null pair={count('normal_null_pairs')}; 追加 row={count('rows_added')}"
        f"\nexecutor: {executor_label(os.environ)}"
        f"\nrun: {sanitize_one_line(run_url(os.environ), 420)}"
    )


def main(argv: list[str] | None = None, *, transport: Transport = _urllib_transport) -> int:
    args = build_parser().parse_args(argv)
    message = render_message(args)
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
