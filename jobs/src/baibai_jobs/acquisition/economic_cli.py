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

from baibai_engine.cli.environment import load_project_env

from baibai_engine.data_collection.acquisition.refresh import SourceRefreshFailure, SourceRefreshSuccess, refresh_sources
from baibai_engine.data_collection.receipts.native_sources.receipt_policy import retain_rechecks
from baibai_engine.data_collection.acquisition.policy import acquisition_groups, source_policy
from baibai_engine.data_collection.catalog import load_sources
from baibai_engine.data_collection.receipts.native_sources.store import DEFAULT_DB_PATH, EconomicSchemaError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-jobs acquire-economic")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list registered external economic sources")
    listing.add_argument("--format", choices=("text", "json"), default="text")
    commands.add_parser("groups", help="list shared provider/rate acquisition groups as JSON")
    refresh = commands.add_parser("refresh", help="acquire and save selected source records")
    refresh.add_argument("source_ids", nargs="*")
    refresh.add_argument("--group", choices=acquisition_groups())
    window = refresh.add_mutually_exclusive_group(required=True)
    window.add_argument("--start", type=date.fromisoformat)
    window.add_argument("--all-history", action="store_true")
    window.add_argument("--recent", action="store_true")
    refresh.add_argument("--end", type=date.fromisoformat, required=True)
    refresh.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "groups":
            print(json.dumps([{"group": group} for group in acquisition_groups()]))
            return 0
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
        source_ids = args.source_ids
        if args.group:
            members = [
                source.source_id
                for source in load_sources().values()
                if source_policy(source).acquisition_group == args.group
            ]
            if any(source_id not in members for source_id in source_ids):
                raise ValueError("selected source does not belong to the acquisition group")
            source_ids = source_ids or members
        if not source_ids:
            raise ValueError("select source IDs or an acquisition group")
        # A long local acquisition can overlap newer canonical publications.
        # Do not discard a real receipt merely because this working copy has its value.
        with retain_rechecks():
            outcomes = refresh_sources(
                source_ids,
                db_path=args.db,
                start=args.start,
                end=args.end,
                recent=args.recent,
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
