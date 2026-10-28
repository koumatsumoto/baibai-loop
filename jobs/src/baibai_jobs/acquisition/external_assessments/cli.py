"""Limited private collection and explicit local-copy import commands."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from baibai_engine.data_collection.receipts.public_pages.capture import LegacyCaptureError
from baibai_engine.data_collection.receipts.public_pages.models import EquityWebSourceResponse
from baibai_engine.storage.observations.schema import SQLiteSchemaError, validate_current_schema


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine external-assessment")
    commands = parser.add_subparsers(dest="command", required=True)
    add_acquisition_commands(commands)
    return parser


def add_acquisition_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    acquisition = commands.add_parser(
        "collect", help="collect a bounded set into a private local source capture"
    )
    acquisition.add_argument("--output", type=Path, required=True)
    acquisition.add_argument("--tickers", nargs="+")
    acquisition.add_argument(
        "--source", choices=("monex", "rakuten", "kabumap", "minkabu"), action="append"
    )
    acquisition.add_argument("--request-budget", type=int, default=9)
    acquisition.add_argument("--time-limit-seconds", type=float, default=60)
    acquisition.add_argument("--request-timeout-seconds", type=float, default=10)
    replay = commands.add_parser(
        "import",
        help="retain captured originals and derive observations in an existing local DB",
    )
    replay.add_argument("--input", type=Path, required=True)
    replay.add_argument("--db", type=Path, required=True)


def main(argv: list[str] | None = None, /) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "collect":
            from baibai_engine.data_collection.receipts.public_pages.capture import (
                EquityWebCapture,
                save_capture,
            )
            from baibai_jobs.acquisition.external_assessments.collection import (
                DEFAULT_TICKERS,
                collect,
            )

            try:
                args.output.lstat()
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError
            received: list[EquityWebSourceResponse] = []
            result = collect(
                args.tickers or DEFAULT_TICKERS,
                sources=args.source or ("monex", "rakuten"),
                request_budget=args.request_budget,
                time_limit_seconds=args.time_limit_seconds,
                request_timeout_seconds=args.request_timeout_seconds,
                on_received_html=received.append,
            )
            selected = tuple(
                outcome.attempt_id
                for outcome in result.outcomes
                if outcome.status == "observed" and outcome.attempt_id is not None
            )
            save_capture(args.output, EquityWebCapture(tuple(received), selected))
            print(json.dumps({"status": "captured", **result.summary()}, sort_keys=True))
            return (
                0
                if result.stop_reason is None
                and all(item.status == "observed" for item in result.outcomes)
                else 1
            )
        from baibai_engine.data_collection.receipts.public_pages.capture import load_capture
        from baibai_engine.data_collection.receipts.public_pages.receipts import read_responses
        from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import (
            decode_attempt,
        )
        from baibai_engine.machine_analysis.observations.external_assessments.store import save_observations
        from baibai_engine.storage.observations.equity_web_import import import_responses

        capture = load_capture(args.input)
        # Import requires an existing, explicitly migrated local copy. Neither
        # an absent file nor an empty SQLite file is a bootstrap request.
        with closing(sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            validate_current_schema(conn)
        originals_inserted = import_responses(args.db, capture.responses)
        observations = []
        with closing(sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            for attempt_id in capture.selected_attempt_ids:
                receipt_ids = tuple(
                    response.receipt_id
                    for response in capture.responses
                    if response.attempt_id == attempt_id
                )
                observations.append(decode_attempt(read_responses(conn, receipt_ids)))
        inserted = save_observations(args.db, observations)
        print(
            json.dumps(
                {
                    "status": "imported",
                    "source_responses_inserted": originals_inserted,
                    "observations_inserted": inserted,
                },
                sort_keys=True,
            )
        )
        return 0
    except FileExistsError:
        print(
            json.dumps({"status": "failed", "category": "staging_destination_exists"}),
            file=sys.stderr,
        )
        return 1
    except LegacyCaptureError:
        print(
            json.dumps({"status": "failed", "category": "legacy_stage_unsupported"}),
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
