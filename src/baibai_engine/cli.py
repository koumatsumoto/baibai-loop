"""Unified command entry point for the Baibai engine."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import yaml

from baibai_engine.appdb import backup_database, connect_rw, database_path, initialize_database

Command = Callable[[list[str] | None], int]


def _usage() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine")
    parser.add_argument(
        "domain",
        choices=(
            "screening",
            "macro",
            "operation",
            "position",
            "proposal",
            "research",
            "task",
            "validate",
            "db",
        ),
    )
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser


def _delegate(domain: str, arguments: list[str]) -> int:
    if domain == "screening":
        from baibai_engine.screening.cli import main

        return main(arguments)
    if domain == "macro":
        if arguments and arguments[0] == "context":
            from baibai_engine.macro.context_cli import main

            return main(arguments[1:])
        from baibai_engine.macro.indicators.cli import main

        return main(arguments)
    if domain == "position":
        from baibai_engine.position.cli import main

        return main(arguments)
    if domain == "operation":
        from baibai_engine.operation.cli import main

        return main(arguments)
    if domain == "proposal":
        from baibai_engine.proposals.cli import main

        return main(arguments)
    if domain == "validate":
        from baibai_engine.validation.cli import main

        return main(arguments)
    if domain == "task":
        from baibai_engine.tasks.cli import main

        return main(arguments)
    if domain == "research":
        if arguments and arguments[0] == "evaluate":
            from baibai_engine.research.decision_cli import main

            return main(arguments[1:])
        from baibai_engine.research.opportunity_cli import main

        return main(arguments)
    if domain == "db":
        return _db_main(arguments)
    raise AssertionError(f"unreachable domain: {domain}")


def _db_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="baibai-engine db")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "backup", "info"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--db", type=Path)
    import_tasks = subparsers.add_parser("import-tasks")
    import_tasks.add_argument("--db", type=Path)
    import_tasks.add_argument("--source", type=Path, default=Path("records/05-task/tasks.yaml"))
    import_screening = subparsers.add_parser("import-screening")
    import_screening.add_argument("--runs-db", type=Path)
    import_screening.add_argument(
        "--source",
        type=Path,
        default=Path("records/02-candidates"),
    )
    import_research = subparsers.add_parser("import-research")
    import_research.add_argument("--db", type=Path)
    import_research.add_argument(
        "--research-source",
        type=Path,
        default=Path("records/03-thesis"),
    )
    import_research.add_argument(
        "--position-source",
        type=Path,
        default=Path("records/04-position"),
    )
    import_ledger = subparsers.add_parser("import-ledger")
    import_ledger.add_argument("--db", type=Path)
    import_ledger.add_argument(
        "--source",
        type=Path,
        default=Path("records/04-position/portfolio-ledger.yaml"),
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            version = initialize_database(args.db)
            print(yaml.safe_dump({"path": str(database_path(args.db)), "user_version": version}))
            return 0
        if args.command == "backup":
            target = backup_database(args.db)
            payload = {"path": None if target is None else str(target), "status": "ok"}
            print(yaml.safe_dump(payload))
            return 0
        if args.command == "import-tasks":
            from baibai_engine.tasks.importer import import_task_file

            inserted, unchanged = import_task_file(args.source, db_path=args.db)
            print(
                yaml.safe_dump(
                    {
                        "source": str(args.source),
                        "inserted": inserted,
                        "unchanged": unchanged,
                    },
                    sort_keys=False,
                )
            )
            return 0
        if args.command == "import-screening":
            from baibai_engine.screening.run_store import import_screening_runs

            inserted, unchanged = import_screening_runs(
                args.source,
                db_path=args.runs_db,
            )
            print(
                yaml.safe_dump(
                    {
                        "source": str(args.source),
                        "inserted": inserted,
                        "unchanged": unchanged,
                    },
                    sort_keys=False,
                )
            )
            return 0
        if args.command == "import-research":
            from baibai_engine.research.importer import import_research_records

            result = import_research_records(
                args.research_source,
                args.position_source,
                db_path=args.db,
            )
            print(
                yaml.safe_dump(
                    {
                        "research_source": str(args.research_source),
                        "position_source": str(args.position_source),
                        "packets_inserted": result.packets_inserted,
                        "packets_unchanged": result.packets_unchanged,
                        "reviews_inserted": result.reviews_inserted,
                        "reviews_unchanged": result.reviews_unchanged,
                        "holding_reviews_inserted": result.holding_reviews_inserted,
                        "holding_reviews_unchanged": result.holding_reviews_unchanged,
                    },
                    sort_keys=False,
                )
            )
            return 0
        if args.command == "import-ledger":
            from baibai_engine.position.importer import import_and_check_ledger

            ledger_result, parity = import_and_check_ledger(args.source, db_path=args.db)
            print(
                yaml.safe_dump(
                    {
                        "source": str(args.source),
                        "events_inserted": ledger_result.events_inserted,
                        "events_unchanged": ledger_result.events_unchanged,
                        "prices_inserted": ledger_result.prices_inserted,
                        "prices_unchanged": ledger_result.prices_unchanged,
                        "meta_inserted": ledger_result.meta_inserted,
                        "meta_unchanged": ledger_result.meta_unchanged,
                        "event_order_matches": parity.event_order_matches,
                        "snapshot_matches": parity.snapshot_matches,
                        "market_prices_match": parity.market_prices_match,
                        "overrides_match": parity.overrides_match,
                        "meta_matches": parity.meta_matches,
                    },
                    sort_keys=False,
                )
            )
            return 0
        path = database_path(args.db)
        if not path.exists():
            print(yaml.safe_dump({"path": str(path), "exists": False}))
            return 0
        with closing(connect_rw(path)) as connection:
            tables = connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall()
            counts = {
                str(row[0]): int(
                    connection.execute(f'SELECT count(*) FROM "{row[0]}"').fetchone()[0]
                )
                for row in tables
            }
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        print(
            yaml.safe_dump(
                {"path": str(path), "exists": True, "user_version": version, "tables": counts},
                sort_keys=False,
            )
        )
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    args = _usage().parse_args(argv)
    return _delegate(args.domain, args.arguments)


if __name__ == "__main__":
    raise SystemExit(main())
