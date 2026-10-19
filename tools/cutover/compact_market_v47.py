"""Candidate-only compact market machine copy of a fixed published L1 release.

The temporary transition tool selects this path only with ``--market-mode compact``.
This module itself never publishes, deletes lake data from the source, or touches
machine objects. An explicit caller supplies a new output path.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

from baibai_engine.storage.lake.datasets import LAKE_DATASETS
from baibai_engine.storage.lake.hydrate import (
    expected_row_totals,
    plan_release_load,
    require_still_current,
)
from baibai_engine.storage.lake.reader import FixedRelease
from baibai_engine.storage.observations.lake_origin import (
    LakeStoreOrigin,
    read_lake_store_origin_from_connection,
)
from baibai_engine.storage.observations.schema import (
    SQLITE_SCHEMA_VERSION,
    validate_current_schema,
)

LOCAL_TABLES = frozenset({"source_coverage", "lake_store_origin"})


def _require_published_source(
    src: sqlite3.Connection,
    *,
    release: FixedRelease,
    still_current: Callable[[], tuple[str, str]] | None,
) -> LakeStoreOrigin:
    """Apply dehydrate's origin and row preconditions in the copy's read snapshot."""
    require_still_current(still_current, release)
    expected_origin = LakeStoreOrigin(release.release_id, release.manifest_sha256)
    if read_lake_store_origin_from_connection(src) != expected_origin:
        raise ValueError("compact source origin is not the fixed published release")
    published = expected_row_totals(
        plan_release_load(release, dataset_names=tuple(sorted(release.dataset_manifests)))[0]
    )
    for name, dataset in sorted(LAKE_DATASETS.items()):
        held = int(src.execute(f'SELECT COUNT(*) FROM "{dataset.sqlite_table}"').fetchone()[0])
        if name not in release.dataset_manifests:
            if held:
                raise ValueError(f"{name} holds {held} row(s) absent from the fixed release")
            continue
        expected = published.get(name, 0)
        if expected != release.dataset_manifest(name).totals.rows:
            raise ValueError(f"{name} release manifest row totals disagree")
        if held != expected:
            raise ValueError(f"{name} holds {held} row(s) but fixed release publishes {expected}")
    return expected_origin


def _schema(conn: sqlite3.Connection) -> tuple[tuple[str, str, str, str | None], ...]:
    return tuple(
        conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table','index','trigger') ORDER BY type, name"
        )
    )


def _ordered_rows(conn: sqlite3.Connection, table: str) -> Iterator[tuple[Any, ...]]:
    if table not in LOCAL_TABLES:
        raise ValueError("unexpected store-local table")
    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    keys = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
    if not keys:
        raise ValueError(f"store-local table has no primary key: {table}")
    order = ", ".join(f'"{key}"' for key in keys)
    yield from conn.execute(f'SELECT * FROM "{table}" ORDER BY {order}')


def _row_digest(rows: list[tuple[Any, ...]]) -> str:
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    ).hexdigest()


def _create_exact_source_schema(src: sqlite3.Connection, output: Path) -> sqlite3.Connection:
    objects = src.execute("SELECT type, name, sql FROM sqlite_master ORDER BY rowid").fetchall()
    if any(kind not in {"table", "index"} for kind, _, _ in objects):
        raise ValueError("source carries a view or trigger")
    statements = [sql for kind, _, sql in objects if kind == "table" and sql is not None]
    statements += [sql for kind, _, sql in objects if kind == "index" and sql is not None]
    if any(not sql.lstrip().upper().startswith("CREATE ") for sql in statements):
        raise ValueError("source schema contains unexpected DDL")
    # The caller has already created this private staging inode exclusively.
    target = sqlite3.connect(f"{output.resolve().as_uri()}?mode=rw", uri=True)
    try:
        target.executescript(
            "BEGIN IMMEDIATE;\n"
            + ";\n".join(statements)
            + f";\nPRAGMA user_version = {SQLITE_SCHEMA_VERSION};\nCOMMIT;"
        )
        validate_current_schema(target)
        return target
    except BaseException:
        target.close()
        raise


def build_compact_market(
    source: Path,
    output: Path,
    *,
    release: FixedRelease,
    still_current: Callable[[], tuple[str, str]] | None = None,
) -> dict[str, object]:
    """Copy two local tables only after validating the full fixed-release source."""
    if not source.is_file() or source.is_symlink() or output.exists() or output.is_symlink():
        raise ValueError("source must exist and output must be a new regular path")
    if source.resolve() == output.resolve():
        raise ValueError("source and output must differ")
    lake_tables = {dataset.sqlite_table for dataset in LAKE_DATASETS.values()}
    with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as src:
        src.execute("PRAGMA query_only = ON")
        src.execute("BEGIN")
        validate_current_schema(src)
        tables = {
            name
            for (name,) in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if tables != lake_tables | LOCAL_TABLES:
            raise ValueError("source has an unrecognized or missing physical table")
        source_schema = _schema(src)
        source_origin = _require_published_source(src, release=release, still_current=still_current)
        if src.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("compact source failed SQLite quick_check")
        if src.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("compact source has foreign key violations")
        fd, staging_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".building", dir=output.parent
        )
        staging = Path(staging_name)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        try:
            target = _create_exact_source_schema(src, staging)
            try:
                if _schema(target) != source_schema:
                    raise ValueError("canonical current schema differs from source")
                source_rows = {
                    name: list(_ordered_rows(src, name)) for name in sorted(LOCAL_TABLES)
                }
                with target:
                    for name, rows in source_rows.items():
                        columns = len(target.execute(f'PRAGMA table_info("{name}")').fetchall())
                        placeholders = ",".join("?" for _ in range(columns))
                        target.executemany(
                            f'INSERT INTO "{name}" VALUES ({placeholders})',
                            rows,
                        )
                validate_current_schema(target)
                if _schema(target) != source_schema:
                    raise ValueError("output schema differs after store-local copy")
                if read_lake_store_origin_from_connection(target) != source_origin:
                    raise ValueError("output origin differs")
                counts: dict[str, int] = {}
                digests: dict[str, str] = {}
                for name, rows in source_rows.items():
                    saved = list(_ordered_rows(target, name))
                    if saved != rows:
                        raise ValueError(f"store-local content differs: {name}")
                    counts[name] = len(rows)
                    digests[name] = _row_digest(rows)
                for name in sorted(lake_tables):
                    if target.execute(f'SELECT EXISTS(SELECT 1 FROM "{name}")').fetchone()[0]:
                        raise ValueError(f"compact output contains lake-owned rows: {name}")
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("compact output failed SQLite quick_check")
                if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("compact output has foreign key violations")
            finally:
                target.close()
            report = {
                "schema": 47,
                "source": str(source.resolve()),
                "output": str(output.resolve()),
                "output_bytes": staging.stat().st_size,
                "origin": {
                    "release_id": source_origin.release_id,
                    "release_manifest_sha256": source_origin.release_manifest_sha256,
                },
                "local_counts": counts,
                "local_row_sha256": digests,
                "empty_lake_tables": len(lake_tables),
                "output_sha256": hashlib.sha256(staging.read_bytes()).hexdigest(),
            }
            # Atomic no-replace publication. Even if another process creates output
            # after the preliminary check, link fails and its bytes stay untouched.
            require_still_current(still_current, release)
            os.link(staging, output, follow_symlinks=False)
            return report
        finally:
            staging.unlink(missing_ok=True)
