"""Limited private collection and explicit local-copy import commands."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from baibai_engine.market.sqlite.schema import SQLiteSchemaError, validate_current_schema

from .collection import DEFAULT_TICKERS, collect, load_staging, save_staging
from .store import save_observations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine retail-dynamics")
    commands = parser.add_subparsers(dest="command", required=True)
    acquisition = commands.add_parser(
        "collect", help="collect a bounded set into private local staging JSON"
    )
    acquisition.add_argument("--output", type=Path, required=True)
    acquisition.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS)
    acquisition.add_argument("--source", choices=("monex", "rakuten", "kabumap"), action="append")
    acquisition.add_argument("--request-budget", type=int, default=9)
    acquisition.add_argument("--time-limit-seconds", type=float, default=60)
    acquisition.add_argument("--request-timeout-seconds", type=float, default=10)
    replay = commands.add_parser(
        "import",
        help="import saved normalized observations into an explicitly selected migrated local DB",
    )
    replay.add_argument("--input", type=Path, required=True)
    replay.add_argument("--db", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None, /) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "collect":
            try:
                args.output.lstat()
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError
            result = collect(
                args.tickers,
                sources=args.source or ("monex", "rakuten"),
                request_budget=args.request_budget,
                time_limit_seconds=args.time_limit_seconds,
                request_timeout_seconds=args.request_timeout_seconds,
            )
            save_staging(args.output, result.observations)
            print(json.dumps({"status": "staged", **result.summary()}, sort_keys=True))
            return (
                0
                if result.stop_reason is None
                and all(item.status == "observed" for item in result.outcomes)
                else 1
            )
        observations = load_staging(args.input)
        # Import requires an existing, explicitly migrated local copy. Neither
        # an absent file nor an empty SQLite file is a bootstrap request.
        with closing(sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            validate_current_schema(conn)
        inserted = save_observations(args.db, observations)
        print(json.dumps({"status": "imported", "observations_inserted": inserted}, sort_keys=True))
        return 0
    except FileExistsError:
        print(
            json.dumps({"status": "failed", "category": "staging_destination_exists"}),
            file=sys.stderr,
        )
        return 1
    except SQLiteSchemaError:
        print(json.dumps({"status": "failed", "category": "schema_failure"}), file=sys.stderr)
        return 1
    except sqlite3.Error:
        print(json.dumps({"status": "failed", "category": "storage_failure"}), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        # Exception messages can contain provider data, paths or input values.
        print(
            json.dumps({"status": "failed", "category": "invalid_input_or_storage"}),
            file=sys.stderr,
        )
        return 1
