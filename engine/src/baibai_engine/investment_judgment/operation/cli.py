"""Writer and resume CLI for operation sessions."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.investment_judgment.operation.models import SESSION_KINDS, OperationPayload
from baibai_engine.investment_judgment.operation.service import OperationService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine operation")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--format", choices=("yaml", "json"), default="yaml")
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser(
        "start",
        help="open a session for one trigger; at most one session is active across all kinds",
    )
    start.add_argument("--kind", choices=SESSION_KINDS, required=True)
    start.add_argument("--as-of", type=date.fromisoformat, required=True)
    start.add_argument("--ticker")
    start.add_argument(
        "--payload",
        type=Path,
        required=True,
        help="path to an OperationPayload YAML or JSON file",
    )

    checkpoint = commands.add_parser(
        "checkpoint",
        help="replace the active session's payload with the current working state",
    )
    checkpoint.add_argument("operation_id")
    checkpoint.add_argument(
        "--payload",
        type=Path,
        required=True,
        help="path to an OperationPayload YAML or JSON file",
    )

    show = commands.add_parser(
        "show",
        help="print one session, or list sessions filtered by status",
    )
    show.add_argument("operation_id", nargs="?")
    show.add_argument("--status", choices=("active", "completed"))

    complete = commands.add_parser(
        "complete",
        help="close an active session once its kind's completion requirements hold",
    )
    complete.add_argument("operation_id")
    complete.add_argument(
        "--payload",
        type=Path,
        required=True,
        help="path to an OperationPayload YAML or JSON file",
    )
    assessment = commands.add_parser(
        "record-assessment", help="bind a completed Reviewed Assessment to its fixed target"
    )
    assessment.add_argument("operation_id")
    assessment.add_argument("--assessment-id", required=True)
    proposal = commands.add_parser(
        "record-proposal", help="bind an immutable published Trade Proposal"
    )
    proposal.add_argument("operation_id")
    proposal.add_argument("--proposal-id", required=True)
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
    try:
        if args.command == "start":
            payload = _load_payload(args.payload)
            operation = service.start(
                session_kind=args.kind,
                as_of=args.as_of,
                ticker=args.ticker,
                started_at=current_time,
                payload=payload,
            )
            _emit(operation.public(), output_format=args.format)
        elif args.command == "checkpoint":
            _emit(
                service.checkpoint(
                    args.operation_id, _load_payload(args.payload), now=current_time
                ).public(),
                output_format=args.format,
            )
        elif args.command == "complete":
            _emit(
                service.complete(
                    args.operation_id,
                    _load_payload(args.payload),
                    completed_at=current_time,
                ).public(),
                output_format=args.format,
            )
        elif args.command == "record-assessment":
            _emit(
                service.record_assessment(
                    args.operation_id, args.assessment_id, now=current_time
                ).public(),
                output_format=args.format,
            )
        elif args.command == "record-proposal":
            _emit(
                service.record_proposal(
                    args.operation_id, args.proposal_id, now=current_time
                ).public(),
                output_format=args.format,
            )
        elif args.command == "record-unfinished":
            _emit(
                service.record_unfinished(
                    args.operation_id,
                    ticker=args.ticker,
                    reason=args.reason,
                    references=tuple(args.ref),
                    now=current_time,
                ).public(),
                output_format=args.format,
            )
        elif args.command == "show":
            if args.operation_id is not None:
                if args.status is not None:
                    raise ValueError("--status cannot be combined with operation_id")
                _emit(service.get(args.operation_id).public(), output_format=args.format)
            else:
                _emit(
                    {
                        "operations": [
                            operation.public() for operation in service.list(status=args.status)
                        ]
                    },
                    output_format=args.format,
                )
        else:  # pragma: no cover
            raise AssertionError(f"unreachable operation command: {args.command}")
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def _load_payload(path: Path) -> OperationPayload:
    loaded = safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("operation payload must be a mapping")
    return OperationPayload.model_validate(loaded)


def _emit(payload: object, *, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        yaml.safe_dump(payload, sys.stdout, sort_keys=False, allow_unicode=True)


if __name__ == "__main__":
    raise SystemExit(main())
