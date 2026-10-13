"""一回限りのMarket Context保存名切替。隔離コピーだけを変更しR2へ書かない。"""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from tools.ops.migrate_market_30_to_31 import _digest, _quoted, _tables

from baibai_engine.storage.application.schema import APPLICATION_SCHEMA_VERSION

TABLE_NAMES = {
    "macro_context": "market_context_assessment",
    "macro_context_head": "market_context_assessment_head",
}


def migrate_copy(source: Path, output: Path) -> dict[str, tuple[int, str]]:
    """全保存行を変えずschema20のread-only sourceから別fileの21を作る。"""
    if output.exists() or output.is_symlink():
        raise FileExistsError("migration destination already exists")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as old:
        old.execute("BEGIN")
        if old.execute("PRAGMA user_version").fetchone()[0] != 20:
            raise ValueError("migration requires application schema 20")
        tables = _tables(old)
        if not set(TABLE_NAMES).issubset(tables) or tables.intersection(TABLE_NAMES.values()):
            raise ValueError("migration requires the original assessment tables")
        before = {table: _digest(old, table) for table in sorted(tables)}
        with closing(sqlite3.connect(output)) as new:
            try:
                output.chmod(0o600)
                old.backup(new)
                new.execute("PRAGMA foreign_keys=ON")
                new.execute("BEGIN IMMEDIATE")
                for previous, current in TABLE_NAMES.items():
                    new.execute(f"ALTER TABLE {_quoted(previous)} RENAME TO {_quoted(current)}")
                new.execute("DROP INDEX macro_context_asof_idx")
                new.execute(
                    "CREATE INDEX market_context_assessment_asof_idx "
                    "ON market_context_assessment(as_of, published_at, context_id)"
                )
                new.execute(f"PRAGMA user_version={APPLICATION_SCHEMA_VERSION}")
                if (
                    new.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                    or new.execute("PRAGMA foreign_key_check").fetchall()
                ):
                    raise ValueError("migration copy integrity failed")
                if _tables(new) != {TABLE_NAMES.get(table, table) for table in tables}:
                    raise ValueError("migration changed the table inventory")
                for table, expected in before.items():
                    if _digest(new, TABLE_NAMES.get(table, table)) != expected:
                        raise ValueError("migration changed saved rows")
                new.commit()
            except BaseException:
                new.rollback()
                output.unlink(missing_ok=True)
                raise
    return before


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rows = migrate_copy(args.source, args.output)
    print(f"application schema21 copy verified: {len(rows)} tables, original payloads unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
