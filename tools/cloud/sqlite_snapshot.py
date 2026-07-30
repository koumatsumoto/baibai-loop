"""Create or validate a consistent SQLite file for R2 transfer."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def validate_database(path: Path) -> None:
    """Fail unless ``path`` is a readable, internally consistent SQLite database."""

    if not path.is_file():
        raise FileNotFoundError(f"SQLite file does not exist: {path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if result is None or result[0] != "ok":
            raise sqlite3.DatabaseError(f"SQLite quick_check failed for {path}: {result}")


def database_schema_version(path: Path) -> int:
    """Return ``PRAGMA user_version`` without opening the store for writes."""
    if not path.is_file():
        raise FileNotFoundError(f"SQLite file does not exist: {path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute("PRAGMA user_version").fetchone()
    if row is None:
        raise sqlite3.DatabaseError(f"SQLite user_version is unavailable: {path}")
    return int(row[0])


def create_snapshot(source: Path, output: Path) -> None:
    """Copy ``source`` through SQLite's backup API, including uncheckpointed WAL rows."""

    if not source.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_db, sqlite3.connect(output) as target_db:
        source_db.backup(target_db)
    validate_database(output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("create")
    snapshot.add_argument("--source", type=Path, required=True)
    snapshot.add_argument("--output", type=Path, required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--path", type=Path, required=True)
    check.add_argument("--schema-version", type=int)
    version = subparsers.add_parser("version")
    version.add_argument("--path", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "create":
        create_snapshot(args.source, args.output)
    elif args.command == "version":
        print(database_schema_version(args.path))
    else:
        validate_database(args.path)
        if args.schema_version is not None:
            actual = database_schema_version(args.path)
            if actual != args.schema_version:
                raise sqlite3.DatabaseError(
                    f"SQLite schema version mismatch for {args.path}: "
                    f"expected={args.schema_version} actual={actual}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
