"""One-shot application 24 -> 25 candidate, without modifying the source.

Remove this tool after the attended PR #1541 cutover. It does not publish anything.
"""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from baibai_engine.storage.application.schema import APPLICATION_SCHEMA_VERSION, SCHEMA_SQL


def prepare(source: Path, output: Path) -> None:
    """Copy a v24 snapshot and remove only its obsolete market-price cache."""
    if APPLICATION_SCHEMA_VERSION != 25:
        raise RuntimeError("this one-shot tool only targets application schema 25")
    # Exclusive creation refuses existing candidates and the source itself.
    with output.open("xb"):
        pass
    try:
        with (
            closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as original,
            closing(sqlite3.connect(output)) as candidate,
            closing(sqlite3.connect(":memory:")) as expected,
        ):
            original.backup(candidate)
            if candidate.execute("PRAGMA user_version").fetchone()[0] != 24:
                raise ValueError("expected an application schema 24 source")
            if candidate.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("source integrity check failed")
            expected.executescript(SCHEMA_SQL)
            candidate.execute("DROP TABLE ledger_market_price")
            # The only DDL change is removal of that cache, not business records.
            query = (
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
            if candidate.execute(query).fetchall() != expected.execute(query).fetchall():
                raise ValueError("source does not have the expected application 24 shape")
            if candidate.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("source has broken application references")
            candidate.execute("PRAGMA user_version = 25")
            candidate.commit()
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    prepare(args.source, args.output)
    print("Prepared application schema 25 candidate; source unchanged; not published.")


if __name__ == "__main__":
    main()
