"""One-shot indicator 7 -> 9 candidate; no scalar-to-native reconstruction.

Remove after the attended PR #1541 cutover. Source and published stores are untouched.
"""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from baibai_engine.market_context.indicators.db import SCHEMA_PATH, validate_current_schema


def prepare(source: Path, output: Path) -> None:
    """Retain every existing row and add an empty native-source table."""
    with output.open("xb"):
        pass
    try:
        with (
            closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as original,
            closing(sqlite3.connect(output)) as candidate,
            closing(sqlite3.connect(":memory:")) as expected,
        ):
            original.backup(candidate)
            if candidate.execute("PRAGMA user_version").fetchone()[0] != 7:
                raise ValueError("expected an indicator schema 7 source")
            if candidate.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("source integrity check failed")
            expected.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            if expected.execute("PRAGMA user_version").fetchone()[0] != 9:
                raise RuntimeError("this one-shot tool only targets indicator schema 9")
            query = (
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' AND name != 'indicator_source_snapshots' "
                "ORDER BY type, name"
            )
            if candidate.execute(query).fetchall() != expected.execute(query).fetchall():
                raise ValueError("source does not have the expected indicator 7 shape")
            native_sql = expected.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'indicator_source_snapshots'"
            ).fetchone()[0]
            candidate.execute(native_sql)
            candidate.execute("PRAGMA user_version = 9")
            validate_current_schema(candidate)
            if candidate.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("source has broken indicator references")
            candidate.commit()
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.source, args.output)
    print("Prepared indicator schema 9 candidate; native sources still require actual originals.")


if __name__ == "__main__":
    main()
