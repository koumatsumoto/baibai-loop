"""List and acquire external economic sources owned by Collection."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path

from baibai_engine.foundation.env import load_project_env

from .economic_acquisition import SourceRefreshFailure, SourceRefreshSuccess, refresh_sources
from .economic_sources import load_sources
from .economic_store import DEFAULT_DB_PATH, EconomicSchemaError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine collection economic")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list registered external economic sources")
    listing.add_argument("--format", choices=("text", "json"), default="text")
    refresh = commands.add_parser("refresh", help="acquire and save selected source records")
    refresh.add_argument("source_ids", nargs="+")
    window = refresh.add_mutually_exclusive_group(required=True)
    window.add_argument("--start", type=date.fromisoformat)
    window.add_argument("--all-history", action="store_true")
    refresh.add_argument("--end", type=date.fromisoformat, required=True)
    refresh.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            sources = sorted(load_sources().values(), key=lambda item: item.source_id)
            if args.format == "json":
                print(json.dumps([asdict(source) for source in sources], ensure_ascii=False))
            else:
                print("source_id\tprovider\tfrequency\tsource_url")
                for source in sources:
                    print(
                        f"{source.source_id}\t{source.provider}\t{source.frequency}\t{source.source_url}"
                    )
            return 0
        load_project_env()
        outcomes = refresh_sources(
            args.source_ids,
            db_path=args.db,
            start=None if args.all_history else args.start,
            end=args.end,
        )
    except (OSError, sqlite3.Error, ValueError, EconomicSchemaError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    failures = []
    for outcome in outcomes:
        if isinstance(outcome, SourceRefreshSuccess):
            print(
                f"{outcome.source_id}\t{outcome.start}\t{outcome.end}\t{outcome.record_count}\tok"
            )
        elif isinstance(outcome, SourceRefreshFailure):
            failures.append(outcome)
            print(f"- {outcome.source_id}: {outcome.message}", file=sys.stderr)
    if failures:
        print(
            f"error: refresh failed for {len(failures)} of {len(outcomes)} sources: "
            + ", ".join(item.source_id for item in failures),
            file=sys.stderr,
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
