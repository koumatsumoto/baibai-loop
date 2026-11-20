"""Writer and resume CLI for operation sessions."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import yaml

from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.primitives.time import JST


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="baibai-engine operation",
        description="Writes return a header. Use show <operation_id> for the complete session.",
    )
    parser.add_argument("--db", type=Path)
    parser.add_argument("--format", choices=("yaml", "json"), default="yaml")
    commands = parser.add_subparsers(dest="command", required=True)

    checkpoint = commands.add_parser(
        "checkpoint", help="update notes, not fixed inputs or outcomes"
    )
    checkpoint.add_argument("operation_id")
    checkpoint.add_argument("--note", required=True)
    checkpoint.add_argument("--next", help="optional next action while active")

    show = commands.add_parser(
        "show",
        help="print one complete session by ID, or list history headers filtered by status",
    )
    show.add_argument("operation_id", nargs="?")
    show.add_argument("--status", choices=("active", "completed"))

    evidence = commands.add_parser(
        "selection-evidence", help="read operational inputs or exact opaque publication evidence"
    )
    evidence.add_argument("operation_id")
    evidence.add_argument("--selection-sha256", required=True)
    evidence.add_argument("--proposal-id")

    complete = commands.add_parser(
        "complete",
        help="close an active session once its kind's completion requirements hold",
    )
    complete.add_argument("operation_id")
    complete.add_argument("--proposal-id", help="exact published proposal for the current outcomes")
    complete.add_argument("--result", required=True, help="summary of the work and its outcome")
    complete.add_argument("--next", required=True, help="next action after this Operation")
    assessment = commands.add_parser(
        "record-assessment", help="bind a completed Reviewed Assessment to its fixed target"
    )
    assessment.add_argument("operation_id")
    assessment.add_argument("--assessment-id", required=True)
    unfinished = commands.add_parser(
        "record-unfinished", help="record unfinished research separately from economic rejection"
    )
    unfinished.add_argument("operation_id")
    unfinished.add_argument("--ticker", required=True)
    unfinished.add_argument("--reason", required=True)
    unfinished.add_argument("--ref", action="append", required=True)
    return parser


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = OperationService(args.db)
    current_time = now or datetime.now(JST)
    output: object
    try:
        if args.command == "selection-evidence":
            from baibai_engine.investment_judgment.workflow.selection_evidence import (
                read_selection_evidence,
            )

            output = read_selection_evidence(
                operation_id=args.operation_id,
                sha256=args.selection_sha256,
                proposal_id=args.proposal_id,
                db_path=args.db,
            )
        elif args.command == "show":
            if args.operation_id is not None:
                if args.status is not None:
                    raise ValueError("--status cannot be combined with operation_id")
                output = service.get(args.operation_id).public()
            else:
                output = {
                    "operations": [item.public() for item in service.list(status=args.status)]
                }
        else:
            if args.command == "checkpoint":
                operation = service.checkpoint(
                    args.operation_id, note=args.note, next=args.next, now=current_time
                )
            elif args.command == "complete":
                operation = service.complete(
                    args.operation_id,
                    proposal_id=args.proposal_id,
                    result=args.result,
                    next=args.next,
                    completed_at=current_time,
                )
            elif args.command == "record-assessment":
                operation = service.record_assessment(
                    args.operation_id, args.assessment_id, now=current_time
                )
            elif args.command == "record-unfinished":
                operation = service.record_unfinished(
                    args.operation_id,
                    ticker=args.ticker,
                    reason=args.reason,
                    references=tuple(args.ref),
                    now=current_time,
                )
            else:  # pragma: no cover
                raise AssertionError(f"unreachable operation command: {args.command}")
            output = operation.model_dump(mode="json", exclude={"payload"})
        _emit(output, output_format=args.format)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def _emit(payload: object, *, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        yaml.safe_dump(payload, sys.stdout, sort_keys=False, allow_unicode=True)


if __name__ == "__main__":
    raise SystemExit(main())
