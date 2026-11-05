"""Copy schema-12 economic receipts into schema 13; never infer lost confirmations.

One-time offline cutover tool. Neither normal acquisition nor integration invokes it.
The original file, receipt clocks and legacy empty records remain unchanged.
"""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from baibai_engine.data_collection.receipts.native_sources.store import (
    RECEIPT_KEYS,
    EconomicSchemaError,
    initialize_database,
)
from baibai_engine.storage.sqlite.merge import require_identical_columns


def migrate(source: Path, output: Path) -> None:
    if output.exists():
        raise FileExistsError(output)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as raw:
        raw.execute("BEGIN")
        if raw.execute("PRAGMA user_version").fetchone()[0] != 12:
            raise EconomicSchemaError("offline migration requires economic schema 12")
        if raw.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'").fetchone():
            raise EconomicSchemaError("unexpected trigger in economic source store")
        with closing(initialize_database(output)):
            pass
        with closing(sqlite3.connect(output.resolve().as_uri(), uri=True)) as target:
            target.execute("ATTACH DATABASE ? AS source", (source.resolve().as_uri() + "?mode=ro",))
            try:
                require_identical_columns(target, tuple(RECEIPT_KEYS))
                with target:
                    for table in RECEIPT_KEYS:
                        target.execute(f"INSERT INTO {table} SELECT * FROM source.{table}")  # nosec B608
            finally:
                target.execute("DETACH DATABASE source")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    migrate(args.source, args.output)
    print("schema-13 copy created; FRED confirmation state requires an explicit fresh recheck")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
