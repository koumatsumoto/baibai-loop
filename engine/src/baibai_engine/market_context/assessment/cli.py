"""明示起動のassessment writerと、保存履歴/分析入力の異なる読取入口。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load

from .models import MarketContextAssessment
from .query import assessment_by_id, assessment_history, latest_assessment
from .service import AssessmentService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine market-context assessment")
    parser.add_argument("--db", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser("publish", help="check or publish one explicit assessment")
    publish.add_argument("draft", type=Path)
    publish.add_argument("--expected-head")
    publish.add_argument("--check", action="store_true")
    commands.add_parser("head", help="read the immutable publication head for CAS")
    show = commands.add_parser("show", help="read an exact historical publication")
    show.add_argument("--context-id", required=True)
    commands.add_parser(
        "history", help="read all saved publication bodies without new-writer validation"
    )
    latest = commands.add_parser("latest", help="select an eligible analytical input")
    latest.add_argument("--asof", type=date.fromisoformat, required=True)
    latest.add_argument("--known-at", type=datetime.fromisoformat)
    return parser


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result: Any
        service = AssessmentService(args.db)
        if args.command == "publish":
            document = MarketContextAssessment.model_validate(safe_load(args.draft.read_text()))
            if args.check:
                if args.expected_head is not None:
                    raise ValueError("--check does not inspect or change the head")
                result = {"check": "ok", "context_id": document.context_id}
            else:
                result = service.publish(document, expected_head=args.expected_head).payload()
        elif args.command == "head":
            result = {"context_id": service.head_id()}
        elif args.command == "show":
            result = assessment_by_id(args.db, context_id=args.context_id).document
        elif args.command == "history":
            result = [record.document for record in assessment_history(args.db)]
        else:
            known_at = args.known_at or now or datetime.now(JST)
            record = latest_assessment(args.db, as_of=args.asof, known_at=known_at)
            result = None if record is None else record.document
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error, KeyError, yaml.YAMLError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
