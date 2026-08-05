"""Unified command entry point for the Baibai Loop engine."""

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
    if domain == "task":
        from baibai_engine.tasks.cli import main

        return main(arguments)
    if domain == "research":
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
        path = database_path(args.db)
        if not path.exists():
            print(yaml.safe_dump({"path": str(path), "exists": False}))
            return 0
        with closing(connect_rw(path)) as connection:
            tables = connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall()
            counts: dict[str, int] = {}
            for row in tables:
                table = str(row[0])
                quoted_table = table.replace('"', '""')
                # SQLite identifiers cannot be bound; the schema name is escaped above.
                count = connection.execute(
                    f'SELECT count(*) FROM "{quoted_table}"'  # nosec B608
                ).fetchone()[0]
                counts[table] = int(count)
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
