"""Explicit as-of Technical Analysis from retained market prices only."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

from baibai_engine.foundation.repository_layout import MARKET_DB_PATH

from .query import technical_analysis


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine technical-analysis")
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser(
        "show", help="calculate SMA20/50/200, BB20, RSI14 and 12-1 momentum without acquisition"
    )
    show.add_argument("--ticker", required=True)
    show.add_argument("--asof", type=date.fromisoformat, required=True)
    show.add_argument("--db", type=Path, default=MARKET_DB_PATH)
    return parser


def main(argv: list[str] | None = None, /) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        result = technical_analysis(args.db, ticker=args.ticker, as_of=args.asof)
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"error: technical analysis read failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0
