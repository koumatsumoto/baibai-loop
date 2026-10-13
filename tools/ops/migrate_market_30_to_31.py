"""一回限りのExternal Assessment改名。隔離SQLite/mirrorだけを更新し、R2へ書かない。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

from baibai_engine.external_assessment.observations.identity import (
    ANALYST_RATING_DATASET,
    ANALYST_RATING_TABLE,
    MODEL_VALUATION_DATASET,
    MODEL_VALUATION_TABLE,
    QUANTS_SCORE_DATASET,
    QUANTS_SCORE_TABLE,
)
from baibai_engine.storage.lake.identity import verified_git_commit
from baibai_engine.storage.lake.models import L1ReleaseSourceRef, load_lake_model_json
from baibai_engine.storage.lake.objects import LocalMirrorSource
from baibai_engine.storage.lake.reader import resolve_release_ref
from baibai_engine.storage.lake.release import create_l1_release
from baibai_engine.storage.lake.writer import export_lake_legacy
from baibai_engine.storage.observations.lake_origin import read_lake_store_origin
from baibai_engine.storage.observations.schema import validate_current_schema

# 旧名はこのtemporary operator toolに限定し、runtimeのreader/writerは新名一本。
TABLE_NAMES = {
    "retail_model_valuation_observations": MODEL_VALUATION_TABLE,
    "retail_analyst_rating_observations": ANALYST_RATING_TABLE,
    "retail_quants_score_observations": QUANTS_SCORE_TABLE,
}
DATASET_NAMES = {
    "retail_dynamics.model_valuation_observations": MODEL_VALUATION_DATASET,
    "retail_dynamics.analyst_rating_observations": ANALYST_RATING_DATASET,
    "retail_dynamics.quants_score_observations": QUANTS_SCORE_DATASET,
}


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _digest(connection: sqlite3.Connection, table: str) -> tuple[int, str]:
    columns = connection.execute(f"PRAGMA table_info({_quoted(table)})").fetchall()
    keys = [column[1] for column in sorted(columns, key=lambda row: row[5]) if column[5]]
    order = keys or [column[1] for column in columns]
    # SQLite metadata identifiers are double-quoted, including embedded quotes.
    query = f"SELECT * FROM {_quoted(table)} ORDER BY {','.join(map(_quoted, order))}"  # nosec B608
    digest, count = hashlib.sha256(), 0
    for row in connection.execute(query):
        digest.update(
            json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
            + b"\n"
        )
        count += 1
    return count, digest.hexdigest()


def migrate_copy(source: Path, output: Path) -> dict[str, tuple[int, str]]:
    """旧schema30の全table・全列を維持し、新schema31の別fileを作る。"""
    if output.exists() or output.is_symlink():
        raise FileExistsError("migration destination already exists")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as old:
        old.execute("BEGIN")
        if old.execute("PRAGMA user_version").fetchone()[0] != 30:
            raise ValueError("migration requires schema 30")
        tables = _tables(old)
        if not set(TABLE_NAMES).issubset(tables) or tables.intersection(TABLE_NAMES.values()):
            raise ValueError("migration requires only the old observation table names")
        before = {table: _digest(old, table) for table in sorted(tables)}
        with closing(sqlite3.connect(output)) as new:
            try:
                output.chmod(0o600)
                old.backup(new)
                new.execute("BEGIN IMMEDIATE")
                for previous, current in TABLE_NAMES.items():
                    new.execute(f"ALTER TABLE {_quoted(previous)} RENAME TO {_quoted(current)}")
                    new.execute(f"DROP INDEX {_quoted(f'idx_{previous}_snapshot')}")
                    new.execute(
                        f"CREATE INDEX {_quoted(f'idx_{current}_snapshot')} "
                        f"ON {_quoted(current)}(snapshot_date)"
                    )
                new.execute("PRAGMA user_version=31")
                validate_current_schema(new)
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


def prepare_release(database: Path, mirror: Path, reference: Path) -> Path:
    """旧固定graphを読み、新名の全履歴releaseをofflineで構築する。pointerは動かさない。"""
    old_ref = load_lake_model_json(reference.read_bytes(), L1ReleaseSourceRef)
    old = resolve_release_ref(LocalMirrorSource(mirror), old_ref)
    origin = read_lake_store_origin(database)
    if origin is None or (origin.release_id, origin.release_manifest_sha256) != (
        old.release_id,
        old.manifest_sha256,
    ):
        raise ValueError("migration source is not bound to the specified old release")
    export = export_lake_legacy(
        sqlite_path=database,
        mirror_root=mirror,
        producer_git_commit=verified_git_commit(),
        expected_store_origin=origin,
    )
    floors = {
        DATASET_NAMES.get(name, name): manifest.coverage_start
        for name, manifest in old.dataset_manifests.items()
    }
    path, _ = create_l1_release(
        dataset_manifest_paths=[item.manifest_path for item in export.datasets.values()],
        mirror_root=mirror,
        published_coverage_start=floors,
    )
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mirror", type=Path)
    parser.add_argument("--release-ref", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.mirror is None) != (args.release_ref is None):
        parser.error("--mirror and --release-ref must be used together")
    try:
        migrate_copy(args.source, args.output)
        result: dict[str, Any] = {
            "status": "migrated_copy",
            "from_schema": 30,
            "to_schema": 31,
            "all_table_rows": "unchanged",
        }
        if args.mirror is not None:
            result["release_manifest"] = str(
                prepare_release(args.output, args.mirror, args.release_ref)
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        print(
            f"External Assessment cutover preparation failed; type={type(error).__name__}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
