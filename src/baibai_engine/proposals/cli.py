"""Human-boundary CLI for current-state trade proposals."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.ledger import PortfolioLedgerError, reconcile_portfolio
from baibai_engine.position.store import LedgerConflictError, LedgerStoreService

from .store import (
    PlannedLimitInput,
    ProposalConflictError,
    ProposalNotFoundError,
    ProposalRecord,
    ProposalStoreService,
    ProposalValidationError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine proposal")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--market-db", type=Path, default=Path("data/screening/market.sqlite"))
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create")
    create.add_argument("--thesis-id", required=True)
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
    try:
        current_time = _operation_instant(now)
        service = ProposalStoreService(
            args.db,
            market_db_path=args.market_db,
            clock=lambda: current_time,
        )
        if args.command == "create":
            raw_input = safe_load(args.input.read_text(encoding="utf-8"))
            planned_limit = PlannedLimitInput.model_validate(raw_input)
            document, append_head = LedgerStoreService(args.db).load_with_head()
            snapshot = reconcile_portfolio(document)
            _emit(
                _public(
                    service.create(
                        args.thesis_id,
                        planned_limit,
                        snapshot,
                        snapshot_append_head=append_head,
                        created_at=current_time,
                    )
                )
            )
        elif args.command == "list":
            _emit({"proposals": [_public(item) for item in service.list(status=args.status)]})
        elif args.command == "show":
            _emit(_public(service.get(args.proposal_id)))
        elif args.command == "decide":
            if args.decision == "approve":
                approval_document, approval_head = LedgerStoreService(args.db).load_with_head()
                approval_snapshot = reconcile_portfolio(approval_document)
            else:
                approval_snapshot = None
                approval_head = None
            _emit(
                _public(
                    service.decide(
                        args.proposal_id,
                        args.decision,
                        decided_at=current_time,
                        snapshot=approval_snapshot,
                        snapshot_append_head=approval_head,
                    )
                )
            )
        else:  # pragma: no cover
            raise AssertionError(f"unreachable proposal command: {args.command}")
    except (
        OSError,
        LedgerConflictError,
        PortfolioLedgerError,
        ProposalConflictError,
        ProposalNotFoundError,
        ProposalValidationError,
        ValidationError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def _operation_instant(now: datetime | None) -> datetime:
    resolved = now or datetime.now(JST)
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise ProposalValidationError("operation clock must be timezone-aware")
    return resolved


def _public(record: ProposalRecord) -> dict[str, object]:
    return {
        "proposal_id": record.proposal_id,
        "ticker": record.ticker,
        "thesis_id": record.thesis_id,
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
