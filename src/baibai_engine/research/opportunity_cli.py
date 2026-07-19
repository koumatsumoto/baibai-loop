"""CLI for opportunity authoring: prepare / status / packet-scaffold /
review-scaffold / promote / plan-limit.

Machine output is YAML on stdout only; human explanation and errors go to stderr.
Exit codes:

- 0 success (``defer`` and "no actionable bargain" are normal judgments)
- 2 argparse / CLI usage error
- 3 missing source / checklist / schema / hash makes the request unprocessable
- 4 output collision, input hash drift, or path-confinement violation
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import TextIO

import yaml

from baibai_engine.foundation.time import JST

from .decision_packet import DecisionPacketError
from .opportunity import (
    OpportunityError,
    compute_status,
    plan_limit,
    prepare_holding_workspace,
    prepare_workspace,
    promote,
    scaffold_packet,
    scaffold_review,
)


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as error:
        raise SystemExit(f"invalid ISO date: {raw}") from error


def _emit(payload: object, out: TextIO) -> None:
    yaml.safe_dump(payload, out, sort_keys=False, allow_unicode=True, default_flow_style=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine research")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser(
        "prepare", help="build the opportunity workspace from a selection output and ledger"
    )
    prepare_parser.add_argument("--asof", required=True, help="workspace as-of date (YYYY-MM-DD)")
    prepare_parser.add_argument("--selection-output", required=True, type=Path)
    prepare_parser.add_argument("--ledger", required=True, type=Path)
    prepare_parser.add_argument("--workspace", required=True, type=Path)
    prepare_parser.add_argument(
        "--force", action="store_true", help="rebuild an existing local workspace"
    )

    holding_prepare_parser = subparsers.add_parser(
        "holding-prepare", help="build a one-ticker workspace for an open holding"
    )
    holding_prepare_parser.add_argument(
        "--asof", required=True, help="workspace as-of date (YYYY-MM-DD)"
    )
    holding_prepare_parser.add_argument("--ledger", required=True, type=Path)
    holding_prepare_parser.add_argument("--ticker", required=True)
    holding_prepare_parser.add_argument("--workspace", required=True, type=Path)
    holding_prepare_parser.add_argument(
        "--force", action="store_true", help="rebuild an existing local workspace"
    )

    status_parser = subparsers.add_parser(
        "status", help="report workspace completion, drift, and the next command"
    )
    status_parser.add_argument("--workspace", required=True, type=Path)

    packet_parser = subparsers.add_parser(
        "packet-scaffold", help="scaffold a packet draft with the previous-day raw close"
    )
    packet_parser.add_argument("--workspace", required=True, type=Path)
    packet_parser.add_argument("--ticker", required=True)
    packet_parser.add_argument("--sqlite-path", required=True, type=Path)
    packet_parser.add_argument(
        "--target-session",
        required=True,
        help="the session the limit is planned for (YYYY-MM-DD); the close is the "
        "latest complete business day strictly before it",
    )
    packet_parser.add_argument("--force", action="store_true")

    review_parser = subparsers.add_parser(
        "review-scaffold", help="scaffold an independent review draft bound to the packet hash"
    )
    review_parser.add_argument("--workspace", required=True, type=Path)
    review_parser.add_argument("--ticker", required=True)
    review_parser.add_argument("--force", action="store_true")

    promote_parser = subparsers.add_parser(
        "promote", help="publish the canonical packet/review to the application DB"
    )
    promote_parser.add_argument("--workspace", required=True, type=Path)
    promote_parser.add_argument("--ticker", required=True)
    promote_parser.add_argument("--db", type=Path)
    promote_parser.add_argument("--packet-id")
    promote_parser.add_argument("--supersedes-id")

    plan_parser = subparsers.add_parser(
        "plan-limit", help="derive a planning-only limit/defer from the previous-day raw close"
    )
    plan_parser.add_argument("--packet", required=True, type=Path)
    plan_parser.add_argument("--ledger", required=True, type=Path)
    plan_parser.add_argument("--sqlite-path", required=True, type=Path)
    plan_parser.add_argument("--target-session", required=True)
    plan_parser.add_argument("--budget-min-yen", type=int, default=200000)
    plan_parser.add_argument("--budget-max-yen", type=int, default=300000)
    plan_parser.add_argument("--output", type=Path)

    return parser


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = sys.stdout
    resolved_now = now or datetime.now(JST)
    try:
        match args.command:
            case "prepare":
                prepared = prepare_workspace(
                    asof=_parse_date(args.asof),
                    selection_output=args.selection_output,
                    ledger=args.ledger,
                    workspace=args.workspace,
                    force=args.force,
                )
                _emit(
                    {
                        "workspace": str(prepared.workspace),
                        "actionable": prepared.actionable,
                        "audit_pool_size": prepared.audit_pool_size,
                        "shortlist_slots": prepared.shortlist_slots,
                        "note": None if prepared.actionable else "no actionable bargain",
                    },
                    out,
                )
            case "holding-prepare":
                prepared = prepare_holding_workspace(
                    asof=_parse_date(args.asof),
                    ledger=args.ledger,
                    ticker=args.ticker,
                    workspace=args.workspace,
                    force=args.force,
                )
                _emit(
                    {
                        "workspace": str(prepared.workspace),
                        "actionable": prepared.actionable,
                        "audit_pool_size": prepared.audit_pool_size,
                        "shortlist_slots": prepared.shortlist_slots,
                    },
                    out,
                )
            case "status":
                _emit(compute_status(args.workspace), out)
            case "packet-scaffold":
                _emit(
                    scaffold_packet(
                        workspace=args.workspace,
                        ticker=args.ticker,
                        sqlite_path=args.sqlite_path,
                        target_session=_parse_date(args.target_session),
                        retrieved_at=resolved_now,
                        force=args.force,
                    ),
                    out,
                )
            case "review-scaffold":
                _emit(
                    scaffold_review(workspace=args.workspace, ticker=args.ticker, force=args.force),
                    out,
                )
            case "promote":
                promoted = promote(
                    workspace=args.workspace,
                    ticker=args.ticker,
                    db_path=args.db,
                    packet_id=args.packet_id,
                    supersedes_id=args.supersedes_id,
                    now=resolved_now,
                )
                _emit(
                    {
                        "packet_id": promoted.packet_id,
                        "review_id": promoted.review_id,
                        "packet_sha256": promoted.packet_sha256,
                    },
                    out,
                )
            case "plan-limit":
                payload = plan_limit(
                    packet=args.packet,
                    ledger=args.ledger,
                    sqlite_path=args.sqlite_path,
                    target_session=_parse_date(args.target_session),
                    budget_min_yen=args.budget_min_yen,
                    budget_max_yen=args.budget_max_yen,
                    now=resolved_now,
                )
                if args.output is not None:
                    from baibai_engine.foundation.filesystem import write_text_atomic

                    write_text_atomic(
                        args.output,
                        yaml.safe_dump(
                            payload, sort_keys=False, allow_unicode=True, default_flow_style=False
                        ),
                    )
                _emit(payload, out)
            case _:  # pragma: no cover - argparse enforces the command set
                raise AssertionError(f"unreachable command: {args.command!r}")
    except OpportunityError as error:
        print(f"error: {error}", file=sys.stderr)
        return error.exit_code
    except DecisionPacketError as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
