"""Explicit schema-29 to schema-30 copy. Never updates the source or a live store."""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from baibai_engine.storage.sqlite.application.schema import APPLICATION_SCHEMA_VERSION


def copy_with_share_conversions(source: Path, destination: Path) -> None:
    """Keep every saved payload/identity and expand only ledger's event-type check."""
    if APPLICATION_SCHEMA_VERSION != 30:
        raise ValueError("this one-time cutover is only for application schema 30")
    source = source.resolve(strict=True)
    if source == destination.resolve():
        raise ValueError("source and destination must differ")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        original.execute("BEGIN")
        if original.execute("PRAGMA user_version").fetchone()[0] != 29:
            raise ValueError("source must have application schema 29")
        if original.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("source integrity check failed")
        sql = original.execute("SELECT sql FROM sqlite_schema WHERE name='ledger_event'").fetchone()
        if (
            sql is None
            or "'tax_confirmed'" not in str(sql[0])
            or "'share_conversion'" in str(sql[0])
        ):
            raise ValueError("source ledger event contract is not the expected schema")
        # Exclusive creation also refuses an existing symlink/destination.
        with destination.open("xb"):
            pass
        try:
            with closing(sqlite3.connect(destination)) as target:
                original.backup(target)
                target.execute("BEGIN IMMEDIATE")
                rows = target.execute("SELECT * FROM ledger_event ORDER BY append_seq").fetchall()
                indexes = target.execute(
                    "SELECT sql FROM sqlite_schema WHERE tbl_name='ledger_event' "
                    "AND type IN ('index','trigger') AND sql IS NOT NULL"
                ).fetchall()
                create = str(sql[0]).replace(
                    "CREATE TABLE ledger_event", "CREATE TABLE ledger_event_new", 1
                )
                create = create.replace("'tax_confirmed'", "'tax_confirmed', 'share_conversion'", 1)
                target.execute(create)
                target.execute("INSERT INTO ledger_event_new SELECT * FROM ledger_event")
                target.execute("DROP TABLE ledger_event")
                target.execute("ALTER TABLE ledger_event_new RENAME TO ledger_event")
                for (statement,) in indexes:
                    target.execute(str(statement))
                if (
                    target.execute("SELECT * FROM ledger_event ORDER BY append_seq").fetchall()
                    != rows
                ):
                    raise ValueError("ledger rows changed during copy")
                if target.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("cutover foreign key check failed")
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("cutover integrity check failed")
                target.execute("PRAGMA user_version=30")
                target.commit()
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    copy_with_share_conversions(args.source, args.destination)


if __name__ == "__main__":
    main()
