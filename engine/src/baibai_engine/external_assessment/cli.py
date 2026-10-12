"""External Assessmentの公開入口。読取と既存L1取得・importを明示的に分ける。"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from baibai_engine.external_assessment.acquisition.cli import add_acquisition_commands
from baibai_engine.external_assessment.acquisition.cli import main as acquisition_main
from baibai_engine.foundation.repository_layout import MARKET_DB_PATH

from .query import external_assessment


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine external-assessment")
    commands = parser.add_subparsers(dest="command", required=True)
    add_acquisition_commands(commands)
    show = commands.add_parser("show", help="read saved assessments without acquisition or writes")
    show.add_argument("--ticker", required=True)
    show.add_argument("--cutoff", required=True, type=datetime.fromisoformat)
    show.add_argument("--evaluated-at", type=datetime.fromisoformat)
    show.add_argument("--db", type=Path, default=MARKET_DB_PATH)
    return parser


def main(argv: list[str] | None = None, /) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] in {"collect", "import"}:
        return acquisition_main(arguments)
    args = build_parser().parse_args(arguments)
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
