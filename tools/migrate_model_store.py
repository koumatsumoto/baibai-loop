"""schema6の機械分析storeを別コピーでschema7へ明示移行する。本番へは送信しない。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from itertools import zip_longest
from pathlib import Path

from baibai_engine.foundation.sqlite_read import connect_read_only
from baibai_engine.storage.machine_analysis import migrate_run_store_v6
from baibai_jobs.storage.sqlite_snapshot import create_snapshot, database_schema_version


def _legacy_counts(path: Path) -> dict[str, int]:
    with closing(connect_read_only(path)) as connection:
        return {
            "screening_run": int(
                connection.execute("SELECT COUNT(*) FROM screening_run").fetchone()[0]
            ),
            "stock_analysis": int(
                connection.execute("SELECT COUNT(*) FROM stock_analysis").fetchone()[0]
            ),
        }


def verify_migration(source: Path, output: Path) -> dict[str, int]:
    """反映後の読戻しも旧行の全fieldを比較し、件数一致だけで済ませない。"""
    if database_schema_version(source) not in (6, 7):
        raise RuntimeError("migration verification requires source schema 6 or 7")
    if database_schema_version(output) != 7:
        raise RuntimeError("migration output must have schema 7")
    with closing(connect_read_only(source)) as before, closing(connect_read_only(output)) as after:
        before.execute("BEGIN")
        after.execute("BEGIN")
        for table, query in (
            ("screening_run", "SELECT * FROM screening_run ORDER BY run_revision_id"),
            ("stock_analysis", "SELECT * FROM stock_analysis ORDER BY run_revision_id,ticker"),
        ):
            if any(
                tuple(left) != tuple(right) if left is not None and right is not None else True
                for left, right in zip_longest(before.execute(query), after.execute(query))
            ):
                raise RuntimeError(f"migration changed legacy rows: {table}")
        if [row[0] for row in after.execute("PRAGMA quick_check")] != ["ok"]:
            raise RuntimeError("migration output failed SQLite quick_check")
        if after.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("migration output has foreign key violations")
    return _legacy_counts(output)


def migrate_copy(source: Path, output: Path) -> dict[str, object]:
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"migration output already exists: {output}")
    create_snapshot(source, output)
    before_version = database_schema_version(output)
    counts = _legacy_counts(output)
    after_version = migrate_run_store_v6(output)
    if verify_migration(source, output) != counts:
        raise RuntimeError("migration changed legacy row counts")
    return {
        "source": str(source),
        "output": str(output),
        "source_schema_version": before_version,
        "output_schema_version": after_version,
        "legacy_row_counts": counts,
        "production_published": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--verify-only", action="store_true", help="compare the read-back store without writing"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = (
            {"verified": True, "legacy_row_counts": verify_migration(args.source, args.output)}
            if args.verify_only
            else migrate_copy(args.source, args.output)
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f"migration failed; inspect output copy before retrying: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
