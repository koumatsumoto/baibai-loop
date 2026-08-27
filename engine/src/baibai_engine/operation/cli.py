"""Writer and resume CLI for operation sessions."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load

from .models import SESSION_KINDS, OperationPayload
from .service import (
    OperationCompletionError,
    OperationConflictError,
    OperationNotFoundError,
    OperationService,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine operation")
    parser.add_argument("--db", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser(
        "start",
        help="open a session for one trigger; at most one session is active across all kinds",
    )
    start.add_argument("--kind", choices=SESSION_KINDS, required=True)
    start.add_argument("--as-of", type=date.fromisoformat, required=True)
    start.add_argument("--ticker")
    start.add_argument("--payload", type=Path, help="path to an OperationPayload YAML or JSON file")

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
    return parser


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = OperationService(args.db)
    current_time = now or datetime.now(JST)
    try:
        if args.command == "start":
            payload = (
                OperationPayload(checkpoint="started", next="record the current checkpoint")
                if args.payload is None
                else _load_payload(args.payload)
            )
            operation = service.start(
                session_kind=args.kind,
                as_of=args.as_of,
                ticker=args.ticker,
                started_at=current_time,
                payload=payload,
            )
            _emit(operation.public())
        elif args.command == "checkpoint":
            _emit(service.checkpoint(args.operation_id, _load_payload(args.payload)).public())
        elif args.command == "complete":
            _emit(
                service.complete(
                    args.operation_id,
                    _load_payload(args.payload),
                    completed_at=current_time,
                ).public()
            )
        elif args.command == "show":
            if args.operation_id is not None:
                if args.status is not None:
                    raise ValueError("--status cannot be combined with operation_id")
                _emit(service.get(args.operation_id).public())
            else:
                _emit(
                    {
                        "operations": [
                            operation.public() for operation in service.list(status=args.status)
                        ]
                    }
                )
        else:  # pragma: no cover
            raise AssertionError(f"unreachable operation command: {args.command}")
    except (
        OSError,
        ValueError,
        ValidationError,
        OperationCompletionError,
        OperationConflictError,
        OperationNotFoundError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def _load_payload(path: Path) -> OperationPayload:
    loaded = safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("operation payload must be a mapping")
    return OperationPayload.model_validate(loaded)


def _emit(payload: object) -> None:
    yaml.safe_dump(payload, sys.stdout, sort_keys=False, allow_unicode=True)


if __name__ == "__main__":
    raise SystemExit(main())
