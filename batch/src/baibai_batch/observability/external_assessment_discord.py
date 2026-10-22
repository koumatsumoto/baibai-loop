"""Dependency-free terminal run notification using only job results and counts."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from baibai_batch.observability.discord import (
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
    "unattempted_pairs",
    "rows_added",
)


def _counter(value: str) -> int | None:
    if value in {"", "unknown"}:
        return None
    if not value.isascii() or not value.isdigit() or len(value) > 12:
        raise argparse.ArgumentTypeError("counter must be nonnegative digits or unknown")
    return int(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="external_assessment_discord")
    for job in ("plan", "acquire", "publish"):
        parser.add_argument(f"--{job}-result", choices=JOB_RESULTS, default="skipped")
    for counter in COUNTERS:
        parser.add_argument(f"--{counter.replace('_', '-')}", type=_counter, default=None)
    parser.add_argument("--cancelled", choices=("true", "false"), default="false")
    parser.add_argument(
        "--terminal-status",
        choices=("pending", "already_published", "already_partially_published"),
        default="pending",
    )
    parser.add_argument(
        "--publication-mode", choices=("complete", "confirmed_partial"), default="complete"
    )
    parser.add_argument("--sources-report", type=Path)
    parser.add_argument("--lease-release-outcome", choices=(*JOB_RESULTS, ""), default="")
    return parser


def render_sources(
    path: Path, *, lease_release: str, cancelled: str, publish_result: str = "skipped"
) -> str:
    """Only allowlisted source names, states and aggregate counts leave the runner."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload["sources"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 4:
            raise ValueError
        statuses = {
            "published",
            "partially_published",
            "already_published",
            "already_partially_published",
            "blocked",
            "incomplete",
            "not_started",
            "publication_unconfirmed",
        }
        labels = {
            "published": "公開確認済み",
            "partially_published": "確定分のみ部分公開・同batch再開なし",
            "already_published": "以前の公開確認済み・今回再公開なし",
            "already_partially_published": "以前の部分公開確認済み・今回再公開なし",
            "blocked": "取得元で停止・未公開",
            "incomplete": "取得未完了・未公開",
            "not_started": "plan未確定・未試行数不明",
            "publication_unconfirmed": "公開確認未完了・未反映とは断定不可",
        }
        names = set()
        lines = []
        degraded = False
        published = False
        previous_only = True
        for row in rows:
            source, status = row["source"], row["status"]
            if (
                source not in {"monex", "rakuten", "kabumap", "minkabu"}
                or source in names
                or status not in statuses
            ):
                raise ValueError
            names.add(source)
            states = row.get("state_counts")
            states = states if isinstance(states, dict) else {}

            def count(value: object) -> str:
                return str(value) if type(value) is int and value >= 0 else "不明"

            typed = row.get("observations")
            if (
                typed is None
                and type(states.get("success")) is int
                and type(states.get("normal_null")) is int
            ):
                typed = states["success"] + states["normal_null"]
            failed = states.get("failure")
            degraded |= status not in {"published", "already_published"} or (
                type(failed) is int and failed > 0
            )
            published |= status in {
                "published",
                "partially_published",
                "already_published",
                "already_partially_published",
            }
            previous_only &= status in {"already_published", "already_partially_published"}
            lines.append(
                f"{source}: {labels[status]}; 固定pair={count(row.get('expected_pairs'))}; "
                f"typed={count(typed)}; 失敗={count(failed)}; "
                f"正常null={count(states.get('normal_null'))}; "
                f"未試行={count(states.get('unattempted'))}; 追加row={count(row.get('rows_added'))}"
            )
        outcome = (
            OUTCOME_CANCELLED
            if cancelled == "true"
            else OUTCOME_FAILED
            if not published
            else OUTCOME_SKIPPED
            if previous_only
            else OUTCOME_DEGRADED
            if degraded
            else OUTCOME_OK
        )
        if publish_result == "failure":
            outcome = OUTCOME_DEGRADED if published else OUTCOME_FAILED
            lines.append("公開処理step失敗・上記のterminal確認済み到達点を保持して確認")
        if lease_release == "failure":
            outcome = OUTCOME_DEGRADED if published else OUTCOME_FAILED
            lines.append("lease解放失敗・上記の公開到達点を保持して確認")
        return (
            f"{OUTCOME_LABELS[outcome]} external assessment\n"
            + "\n".join(lines)
            + f"\nrun: {sanitize_one_line(run_url(os.environ), 420)}"
        )
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        outcome = OUTCOME_CANCELLED if cancelled == "true" else OUTCOME_FAILED
        return (
            f"{OUTCOME_LABELS[outcome]} external assessment — "
            "source別結果不明・公開の未反映とは断定不可\n"
            f"run: {sanitize_one_line(run_url(os.environ), 420)}"
        )


def render_message(args: argparse.Namespace) -> str:
    if args.sources_report is not None:
        return render_sources(
            args.sources_report,
            lease_release=args.lease_release_outcome,
            cancelled=args.cancelled,
            publish_result=args.publish_result,
        )
    previous = args.terminal_status != "pending" and args.plan_result == "success"
    if previous:
        outcome = OUTCOME_SKIPPED
        detail = (
            "以前の部分公開確認済み・今回の再取得/再公開なし"
            if args.terminal_status == "already_partially_published"
            else "以前の公開確認済み・今回の再取得/再公開なし"
        )
    elif args.cancelled == "true" or "cancelled" in (
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
            and (args.typed_pairs + args.known_failure_pairs + args.unattempted_pairs)
            == args.expected_pairs
            and args.normal_null_pairs <= args.typed_pairs
            and args.rows_added <= args.typed_pairs
        )
        if args.publication_mode == "confirmed_partial":
            outcome, detail = OUTCOME_DEGRADED, "確定観測のみ部分公開確認済み・batch 取得再開なし"
            if not consistent_counts:
                detail += "・品質件数不明"
        elif not consistent_counts or args.unattempted_pairs != 0:
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
        value = getattr(args, name) if args.publish_result == "success" or previous else None
        return str(value) if value is not None else "不明"

    return (
        f"{OUTCOME_LABELS[outcome]} external assessment — {detail}"
        f"\n固定 pair={count('expected_pairs')}; typed pair={count('typed_pairs')}; "
        f"既知失敗 pair={count('known_failure_pairs')}; "
        f"全評価値 null pair={count('normal_null_pairs')}; 追加 row={count('rows_added')}"
        f"\n未記録 pair={count('unattempted_pairs')}"
        f"\nexecutor: {executor_label(os.environ)}"
        f"\nrun: {sanitize_one_line(run_url(os.environ), 420)}"
    )


def main(argv: list[str] | None = None, *, transport: Transport = _urllib_transport) -> int:
    args = build_parser().parse_args(argv)
    message = render_message(args)
    print(message, flush=True)
    # Mark before POST: a lost response may already have delivered the message.
    # The fallback must not repeat a possibly completed external send.
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as stream:
            print("attempted=true", file=stream)
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
