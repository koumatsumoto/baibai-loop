"""Move a downloaded store copy forward to the schema this code writes.

The macro merge requires its source and target at the same schema version, and the source
is the copy R2 holds. A macro store published before a migration landed is therefore
unmergeable, and the only thing that moves the published copy forward is the daily
batch — so every schema change would block publishing from a developer machine until
the cloud had run, which is the wrong way round for a store whose deep history is
built locally.

This runs against the throwaway copy in the transfer staging directory, so the object in
R2 is untouched until the merged result is uploaded. A store older than the migration
path's baseline still fails here.
"""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from baibai_engine.batch_api import (
    MARKET_SCHEMA_VERSION,
    validate_market_schema,
)
from baibai_engine.batch_api import (
    open_macro_store as open_indicator_store,
)

MARKET_SOURCE_SCHEMA_VERSION = 26
MARKET_V27_COLUMNS = (
    "ocf_receivables_cash_effect",
    "ocf_inventories_cash_effect",
    "ocf_payables_cash_effect",
    "ocf_contract_liabilities_cash_effect",
    "ocf_advances_received_cash_effect",
    "ocf_other_payables_cash_effect",
    "capex_ppe_reported",
    "capex_intangible_reported",
)


def migrate_indicator_store(path: Path) -> int:
    with closing(open_indicator_store(path)) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def migrate_market_store(path: Path) -> int:
    """Move only the known schema-26 predecessor to schema 27 in a staging copy."""
    connection = sqlite3.connect(path)
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == MARKET_SCHEMA_VERSION:
            validate_market_schema(connection)
            return version
        if version != MARKET_SOURCE_SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported market SQLite schema: {version}; expected "
                f"{MARKET_SOURCE_SCHEMA_VERSION} or {MARKET_SCHEMA_VERSION}"
            )
        connection.execute("BEGIN IMMEDIATE")
        try:
            for column in MARKET_V27_COLUMNS:
                connection.execute(f"ALTER TABLE edinet_metrics ADD COLUMN {column} REAL")
            connection.execute(f"PRAGMA user_version = {MARKET_SCHEMA_VERSION}")
            validate_market_schema(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return MARKET_SCHEMA_VERSION
    finally:
        connection.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True, help="store copy to migrate in place")
    parser.add_argument("--store", choices=("macro", "market"), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.path.is_file():
        raise FileNotFoundError(f"SQLite file does not exist: {args.path}")
    migrate = migrate_indicator_store if args.store == "macro" else migrate_market_store
    print(f"migrated {args.store} copy to schema {migrate(args.path)}: {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
