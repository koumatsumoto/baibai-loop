"""Create a compact upload copy without moving already published tables through the disk."""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Callable, Mapping, Set
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from baibai_engine.storage.snapshots.contracts import LakeDataset
from baibai_engine.storage.snapshots.hydrate import (
    DehydrateReport,
    _dehydrate_copy,
    durable_replace,
)
from baibai_engine.storage.snapshots.reader import FixedRelease
from baibai_engine.storage.sqlite.snapshot import backup_to_new_file, validate_snapshot
from baibai_engine.storage.sqlite.source_cache.schema import (
    SQLITE_SCHEMA_VERSION,
    transfer_schema_shape,
)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _project(source: Path, output: Path, omit: Set[str]) -> tuple[dict[str, int], int] | None:
    """None means unsupported physical schema, not bad data or a failed transfer."""
    with closing(sqlite3.connect(output, timeout=30, uri=True)) as db:
        # The working file may have committed WAL frames. Pin the schema, all counts,
        # the release origin and every copied value in this one read transaction.
        db.execute("ATTACH DATABASE ? AS src", (source.resolve().as_uri() + "?mode=ro",))
        db.execute("BEGIN")
        schema = db.execute(
            "SELECT type,name,tbl_name,sql FROM src.sqlite_schema ORDER BY rowid"
        ).fetchall()
        known, table_columns = transfer_schema_shape()
        if (
            frozenset((k, n, t, sql if k == "index" else None) for k, n, t, sql in schema) != known
            or db.execute("PRAGMA src.user_version").fetchone()[0] != SQLITE_SCHEMA_VERSION
            or any(
                db.execute(f"PRAGMA src.{key}").fetchone()
                != db.execute(f"PRAGMA main.{key}").fetchone()
                for key in ("page_size", "encoding", "auto_vacuum")
            )
        ):
            return None
        tables = {name: sql for kind, name, _, sql in schema if kind == "table"}
        # Only ordinary rowid tables/columns and the official indexes are copied.
        # Extension tables, sequence state and alternate schemas use SQLite's backup.
        if not omit <= tables.keys() or any(
            kind != "table" or wr or strict
            for _, name, kind, _, wr, strict in db.execute("PRAGMA src.table_list")
            if name in tables
        ):
            return None
        columns: dict[str, list[str]] = {}
        for name in tables:
            info = db.execute(f"PRAGMA src.table_xinfo({_quote(name)})").fetchall()
            if tuple(info) != table_columns[name] or any(
                row[-1] or row[1].lower() == "rowid" for row in info
            ):
                return None
            columns[name] = [row[1] for row in info]
        if db.execute("PRAGMA src.quick_check").fetchone() != ("ok",):
            raise sqlite3.DatabaseError("working market SQLite quick_check failed")
        omitted: dict[str, int] = {}
        for name, ddl in tables.items():
            db.execute(ddl)
            ident = _quote(name)
            if name in omit:
                omitted[name] = int(db.execute(f"SELECT count(*) FROM src.{ident}").fetchone()[0])  # nosec B608
            else:
                names = ",".join(map(_quote, columns[name]))
                db.execute(
                    f"INSERT INTO {ident}(rowid,{names}) SELECT rowid,{names} FROM src.{ident}"  # nosec B608
                )
        for kind, _, _, ddl in schema:
            if kind == "index" and ddl is not None:
                db.execute(ddl)
        for field in ("user_version", "application_id"):
            number = int(db.execute(f"PRAGMA src.{field}").fetchone()[0])
            db.execute(f"PRAGMA main.{field}={number}")
        size = (
            db.execute("PRAGMA src.page_size").fetchone()[0]
            * db.execute("PRAGMA src.page_count").fetchone()[0]
        )
        wal = db.execute("PRAGMA src.journal_mode").fetchone() == ("wal",)
        db.commit()
        if wal:
            db.execute("PRAGMA main.journal_mode=WAL")
        return omitted, size


def make_market_transfer(
    source: Path,
    output: Path,
    *,
    release: FixedRelease,
    catalog: Mapping[str, LakeDataset],
    still_current: Callable[[], tuple[str, str]] | None = None,
    prepare_copy: Callable[[Path], None] | None = None,
) -> DehydrateReport:
    """Keep source untouched. A schema fallback does not relax the publication checks."""
    if not source.is_file():
        raise FileNotFoundError("working market SQLite does not exist")
    if source.resolve() == output.resolve() or (output.exists() and source.samefile(output)):
        raise ValueError("market transfer output must be separate from its source")
    if any(Path(str(output) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("market transfer output is not a closed upload file")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".market-transfer-") as work:
        copy = Path(work) / "transfer.sqlite"
        projected = _project(
            source,
            copy,
            {
                dataset.sqlite_table
                for name, dataset in catalog.items()
                if name in release.dataset_manifests
            },
        )
        omitted: dict[str, int]
        if projected is None:
            copy.unlink()
            backup_to_new_file(source, copy)
            omitted, size = {}, copy.stat().st_size
        else:
            omitted, size = projected
        if prepare_copy is not None:
            prepare_copy(copy)
        report = _dehydrate_copy(
            copy, catalog=catalog, release=release, still_current=still_current, omitted=omitted
        )
        validate_snapshot(copy)
        durable_replace(copy, output)
    return replace(report, path=output, bytes_before=size)
