"""One-time additive migration on a separate market SQLite copy; never a daily upgrade."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from baibai_engine.market.retail_dynamics.contract import QUANTS_SCORE_DDL
from baibai_engine.market.sqlite.schema import validate_current_schema


def migrate_copy(source: Path, output: Path) -> None:
    if output.exists():
        raise FileExistsError("migration destination already exists")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as old:
        if old.execute("PRAGMA user_version").fetchone()[0] != 29:
            raise ValueError("migration requires schema 29")
        with closing(sqlite3.connect(output)) as new:
            try:
                output.chmod(0o600)
                old.backup(new)
                new.executescript("BEGIN IMMEDIATE;" + QUANTS_SCORE_DDL + "PRAGMA user_version=30;")
                validate_current_schema(new)
                if new.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("migration copy failed integrity check")
                new.commit()
            except Exception:
                new.rollback()
                output.unlink(missing_ok=True)
                raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        migrate_copy(args.source, args.output)
        print(json.dumps({"status": "migrated_copy", "from_schema": 29, "to_schema": 30}))
        return 0
    except Exception as error:
        print(f"Market migration failed; type={type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
