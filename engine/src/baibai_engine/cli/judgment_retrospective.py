"""旧判断のcontextと執筆draftを一度で読み、照合・学びを保存して再訪する。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

import yaml

from baibai_engine.investment_judgment.judgment_retrospective.document import RetrospectiveDraft
from baibai_engine.investment_judgment.judgment_retrospective.query import (
    get_retrospective,
    list_retrospectives,
    scaffold_retrospective,
)
from baibai_engine.investment_judgment.judgment_retrospective.store import (
    RetrospectiveConflictError,
    RetrospectiveStoreService,
)
from baibai_engine.primitives.time import JST
from baibai_engine.primitives.yaml import safe_load
from baibai_engine.storage.filesystem import write_text_atomic


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="baibai-engine investment-judgment judgment-retrospective"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    scaffold = commands.add_parser(
        "scaffold", help="read an exact old judgment and prepare its comparison draft"
    )
    origin = scaffold.add_mutually_exclusive_group(required=True)
    origin.add_argument("--proposal-id")
    origin.add_argument("--assessment-id")
    scaffold.add_argument("--ticker", help="required for a Proposal item; inferred for Assessment")
    scaffold.add_argument("--asof", required=True, type=date.fromisoformat)
    scaffold.add_argument("--milestone", default="", help="the event or question revisited")
    scaffold.add_argument(
        "--retrospective-id", help="new immutable identity (default: generated UUID)"
    )
    save = commands.add_parser("save", help="append the authored comparison and optional lesson")
    save.add_argument("draft", type=Path, help="edited scaffold output or its draft object")
    get = commands.add_parser("get", help="read one exact record and its fixed judgment context")
    get.add_argument("--retrospective-id", required=True)
    get.add_argument("--sha256", help="require this exact payload digest")
    recent = commands.add_parser("list", help="read finite recent lessons, newest recorded first")
    recent.add_argument("--ticker")
    recent.add_argument("--limit", type=int, default=20, help="1 to 100 records (default: 20)")
    for command in (scaffold, save, get, recent):
        command.add_argument("--db", type=Path)
        command.add_argument("--out", type=Path, help="create a new output file; never overwrite")
    return parser


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    args = build_parser().parse_args(argv)
    instant = now or datetime.now(JST)
    try:
        if args.out is not None and args.out.exists():
            raise RetrospectiveConflictError(f"output already exists: {args.out}")
        payload: object
        match args.command:
            case "scaffold":
                payload = scaffold_retrospective(
                    retrospective_id=args.retrospective_id or f"judgment-retrospective-{uuid4()}",
                    ticker=args.ticker,
                    as_of=args.asof,
                    milestone=args.milestone,
                    proposal_id=args.proposal_id,
                    assessment_id=args.assessment_id,
                    db_path=args.db,
                    known_at=instant,
                )
            case "save":
                authored = safe_load(args.draft.read_text(encoding="utf-8"))
                # The context is read-only authoring material, not a second input authority.
                if isinstance(authored, dict) and "draft" in authored:
                    authored = authored["draft"]
                draft = RetrospectiveDraft.model_validate(authored)
                payload = (
                    RetrospectiveStoreService(args.db, clock=lambda: instant)
                    .save(draft)
                    .model_dump(mode="json")
                )
            case "get":
                payload = get_retrospective(
                    retrospective_id=args.retrospective_id,
                    db_path=args.db,
                    sha256=args.sha256,
                )
            case "list":
                payload = list_retrospectives(db_path=args.db, ticker=args.ticker, limit=args.limit)
            case _:
                raise AssertionError(f"unreachable command: {args.command}")
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if args.out is not None:
            write_text_atomic(args.out, content)
        sys.stdout.write(content)
        return 0
    except RetrospectiveConflictError as error:
        print(f"error: {error}", file=sys.stderr)
        return 4
    except (ValueError, RuntimeError, OSError, sqlite3.Error, yaml.YAMLError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
