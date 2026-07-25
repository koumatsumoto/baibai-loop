"""Macro-context writer and query CLI."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import yaml
from pydantic import ValidationError

from baibai_engine.foundation.yaml_io import safe_load

from .models import MacroContextDocument
from .service import (
    MacroContextConflictError,
    MacroContextNotFoundError,
    MacroContextService,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine macro context")
    parser.add_argument("--db", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("draft", type=Path)
    publish.add_argument("--expected-head")
    show = commands.add_parser("show")
    selection = show.add_mutually_exclusive_group(required=True)
    selection.add_argument("--latest", action="store_true")
    selection.add_argument("--context-id")
    show.add_argument("--asof", required=True, type=date.fromisoformat)
    commands.add_parser("head")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = MacroContextService(args.db)
    try:
        if args.command == "publish":
            raw = safe_load(args.draft.read_text(encoding="utf-8"))
            document = MacroContextDocument.model_validate(raw)
            _emit(service.publish(document, expected_head=args.expected_head).payload())
        elif args.command == "show":
            if args.latest:
                latest_document = service.latest_for(args.asof)
                _emit(None if latest_document is None else latest_document.payload())
            else:
                _emit(service.get_for(args.context_id, as_of=args.asof).payload())
        elif args.command == "head":
            _emit({"context_id": service.head_id()})
        else:  # pragma: no cover
            raise AssertionError(f"unreachable macro context command: {args.command}")
    except (
        OSError,
        ValueError,
        ValidationError,
        MacroContextConflictError,
        MacroContextNotFoundError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def _emit(payload: object) -> None:
    yaml.safe_dump(payload, sys.stdout, sort_keys=False, allow_unicode=True)


__all__ = ["main"]
