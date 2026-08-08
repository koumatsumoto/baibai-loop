"""Maintenance CLI for the application database."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import yaml

from .write import backup_database, connect_rw, database_path, initialize_database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine db")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser(
        "init",
        help="create or migrate the application database and print its schema version",
    )
    init.add_argument("--db", type=Path)
    backup = subparsers.add_parser(
        "backup",
        help="copy the application database to a sibling backup path",
    )
    backup.add_argument("--db", type=Path)
    info = subparsers.add_parser(
        "info",
        help="print the database path, schema version, and per-table row counts",
    )
    info.add_argument("--db", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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


if __name__ == "__main__":
    raise SystemExit(main())
