"""Move a downloaded store copy forward to the schema this code writes.

The merges require their source and target at the same schema version, and the source
is the copy R2 holds. A store published before a migration landed is therefore
unmergeable, and the only thing that moves the published copy forward is the daily
batch — so every schema change would block publishing from a developer machine until
the cloud had run, which is the wrong way round for a store whose deep history is
built locally.

Opening the store is what migrates it. This runs against the throwaway copy in the
transfer staging directory, so the object in R2 is untouched until the merged result
is uploaded. A store older than the migration path's baseline still fails here.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path

from baibai_engine.macro.indicators.db import open_connection as open_indicator_store
from baibai_engine.market.sqlite import open_connection as open_market_store


def migrate_market_store(path: Path) -> int:
    with closing(open_market_store(path)) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def migrate_indicator_store(path: Path) -> int:
    with closing(open_indicator_store(path)) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True, help="store copy to migrate in place")
    parser.add_argument("--store", choices=("market", "macro"), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.path.is_file():
        raise FileNotFoundError(f"SQLite file does not exist: {args.path}")
    migrate = migrate_market_store if args.store == "market" else migrate_indicator_store
    print(f"migrated {args.store} copy to schema {migrate(args.path)}: {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
