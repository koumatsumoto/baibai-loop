"""Temporary attended schema-27 -> 28 copy for Issue #1317; remove after cutover."""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from baibai_engine.market.sqlite.schema import validate_current_schema
from baibai_engine.market.sqlite.snapshot import create_snapshot
from baibai_engine.market.tradingview.contract import DDL, TABLE


def cutover(source: Path, output: Path) -> None:
    """Keep the source unchanged; reject any version except the exact predecessor."""
    if create_snapshot(source, output) != 27:
        output.unlink()
        raise ValueError("cutover requires schema 27")
    try:
        with closing(sqlite3.connect(output)) as conn, conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
                raise ValueError("schema 27 must not contain the TradingView table")
            conn.execute("BEGIN IMMEDIATE")
            for statement in DDL.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("PRAGMA user_version=28")
            validate_current_schema(conn)
            if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("cutover integrity check failed")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("cutover foreign key check failed")
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cutover(args.source, args.output)
    print("schema 28 copy validated; source unchanged")


if __name__ == "__main__":
    main()
