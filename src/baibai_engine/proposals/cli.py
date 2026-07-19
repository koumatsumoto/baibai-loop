"""Human-boundary CLI for current-state trade proposals."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml

from baibai_engine.foundation.time import JST
from baibai_engine.position.ledger import PortfolioLedgerError, reconcile_portfolio
from baibai_engine.position.store import LedgerConflictError, LedgerStoreService
from baibai_engine.research.execution_policy import (
    ExecutionPolicyError,
    load_execution_policy_input,
)

from .store import (
    ProposalConflictError,
    ProposalNotFoundError,
    ProposalRecord,
    ProposalStoreService,
    ProposalValidationError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine proposal")
    parser.add_argument("--db", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create")
    create.add_argument("--packet-id", required=True)
    create.add_argument("--input", type=Path, required=True)

    listing = commands.add_parser("list")
    listing.add_argument("--status", choices=("pending", "approved", "deferred", "rejected"))

    show = commands.add_parser("show")
    show.add_argument("proposal_id")

    decide = commands.add_parser("decide")
    decide.add_argument("proposal_id")
    decide.add_argument("--decision", choices=("approve", "defer", "reject"), required=True)
    return parser


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = ProposalStoreService(args.db)
    current_time = now or datetime.now(JST)
    try:
        if args.command == "create":
            execution_input = load_execution_policy_input(args.input)
            snapshot = reconcile_portfolio(LedgerStoreService(args.db).load())
            _emit(
                _public(
                    service.create(
                        args.packet_id,
                        execution_input,
                        snapshot,
                        created_at=current_time,
                    )
                )
            )
        elif args.command == "list":
            _emit({"proposals": [_public(item) for item in service.list(status=args.status)]})
        elif args.command == "show":
            _emit(_public(service.get(args.proposal_id)))
        elif args.command == "decide":
            approval_snapshot = (
                reconcile_portfolio(LedgerStoreService(args.db).load())
                if args.decision == "approve"
                else None
            )
            _emit(
                _public(
                    service.decide(
                        args.proposal_id,
                        args.decision,
                        decided_at=current_time,
                        snapshot=approval_snapshot,
                    )
                )
            )
        else:  # pragma: no cover
            raise AssertionError(f"unreachable proposal command: {args.command}")
    except (
        OSError,
        ExecutionPolicyError,
        LedgerConflictError,
        PortfolioLedgerError,
        ProposalConflictError,
        ProposalNotFoundError,
        ProposalValidationError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def _public(record: ProposalRecord) -> dict[str, object]:
    return {
        "proposal_id": record.proposal_id,
        "ticker": record.ticker,
        "packet_id": record.packet_id,
        "review_id": record.review_id,
        "created_at": record.created_at.isoformat(),
        "status": record.status,
        "decided_at": None if record.decided_at is None else record.decided_at.isoformat(),
        "payload": dict(record.payload),
    }


def _emit(payload: object) -> None:
    yaml.safe_dump(payload, sys.stdout, sort_keys=False, allow_unicode=True)


if __name__ == "__main__":
    raise SystemExit(main())
