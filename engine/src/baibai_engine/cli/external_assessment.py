"""External Assessmentの公開入口。読取・取得・明示的なoffline再構築を分ける。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from baibai_engine.storage.paths import MARKET_DB_PATH
from baibai_engine.machine_analysis.generation.runners.external_assessment import external_assessment
from baibai_engine.storage.observations.schema import SQLiteSchemaError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine external-assessment")
    commands = parser.add_subparsers(dest="command", required=True)
    rebuild = commands.add_parser(
        "rebuild-raw", help="reparse explicitly retained attempts in an existing market SQLite"
    )
    rebuild.add_argument("--db", type=Path, required=True)
    rebuild.add_argument("--attempt-id", action="append", required=True)
    show = commands.add_parser("show", help="read saved assessments without acquisition or writes")
    show.add_argument("--ticker", required=True)
    show.add_argument("--cutoff", required=True, type=datetime.fromisoformat)
    show.add_argument("--evaluated-at", type=datetime.fromisoformat)
    show.add_argument("--db", type=Path, default=MARKET_DB_PATH)
    return parser


def main(argv: list[str] | None = None, /) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(arguments)
    if args.command == "rebuild-raw":
        from baibai_engine.machine_analysis.observations.external_assessments.rebuild import rebuild_attempts

        try:
            rebuilt = rebuild_attempts(args.db, attempt_ids=args.attempt_id)
        except (ValueError, SQLiteSchemaError, OSError, sqlite3.Error):
            print("error: external assessment raw rebuild failed", file=sys.stderr)
            return 1
        print(json.dumps({"status": "rebuilt", "observations_rebuilt": rebuilt}, sort_keys=True))
        return 0
    try:
        result = external_assessment(
            args.db,
            ticker=args.ticker,
            cutoff=args.cutoff,
            evaluated_at=args.evaluated_at or args.cutoff,
        )
        print(result.model_dump_json(indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"error: external assessment read failed: {error}", file=sys.stderr)
        return 1
