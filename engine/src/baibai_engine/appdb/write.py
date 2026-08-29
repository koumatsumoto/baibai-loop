"""Writable application database connections and schema lifecycle."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from baibai_engine.foundation.time import JST

from .paths import DEFAULT_DB_PATH as DEFAULT_DB_PATH
from .paths import database_path as database_path
from .schema import APPLICATION_SCHEMA_VERSION, SCHEMA_SQL


def connect_rw(path: Path | None = None) -> sqlite3.Connection:
    """Open a writable connection with domain integrity checks enabled."""
    resolved = database_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(resolved, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


KEPT_BACKUP_GENERATIONS = 10
"""How many local checkpoints survive.

Ten covers several judgment-writing sessions, after which an older copy would restore a
different portfolio rather than repair the current one.
"""

_BACKUP_GLOB = "baibai-*.sqlite"


def initialize_database(
    path: Path | None = None,
) -> int:
    """Create the current schema or reject a store that needs an explicit cutover."""
    with closing(connect_rw(path)) as connection:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current == APPLICATION_SCHEMA_VERSION:
            return current
        tables = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if current != 0 or tables is not None:
            raise RuntimeError(
                "application database requires an explicit semantic cutover "
                f"(found user_version={current}, expected={APPLICATION_SCHEMA_VERSION})"
            )
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executescript(SCHEMA_SQL)
            connection.execute(f"PRAGMA user_version = {APPLICATION_SCHEMA_VERSION}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return APPLICATION_SCHEMA_VERSION


def backup_database(
    path: Path | None = None,
    *,
    backup_dir: Path | None = None,
    now: datetime | None = None,
    keep: int = KEPT_BACKUP_GENERATIONS,
) -> Path | None:
    """Create a consistent SQLite snapshot, including uncheckpointed WAL rows.

    Older generations are pruned only once this one is written and verified, so a run
    that cannot produce a checkpoint never reduces the ones already held.
    """
    source_path = database_path(path)
    if not source_path.exists():
        return None
    target_dir = backup_dir or source_path.parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(JST)).strftime("%Y%m%dT%H%M%S%f%z")
    target = target_dir / f"baibai-{stamp}.sqlite"
    with sqlite3.connect(source_path) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("backup integrity_check failed")
        if destination.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.IntegrityError("backup foreign_key_check failed")
    prune_backups(target_dir, keep=keep)
    return target


def prune_backups(backup_dir: Path, *, keep: int = KEPT_BACKUP_GENERATIONS) -> list[Path]:
    """Drop all but the newest ``keep`` checkpoints and return what was removed.

    Ordering is by name: the stamp is fixed-width and its offset is a constant, so the
    lexicographic order is the chronological one. Reading the times off the filesystem
    would instead reorder the set whenever a copy was touched.
    """

    if keep < 1:
        raise ValueError(f"keep must be at least 1: {keep}")
    generations = sorted(backup_dir.glob(_BACKUP_GLOB), key=lambda item: item.name)
    removed = generations[: max(len(generations) - keep, 0)]
    for path in removed:
        path.unlink()
    return removed
