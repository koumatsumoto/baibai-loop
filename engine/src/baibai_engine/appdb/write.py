"""Writable application database connections and schema lifecycle."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime
from pathlib import Path

from baibai_engine.foundation.time import JST

from .migrations import MIGRATIONS, Migration
from .paths import DEFAULT_DB_PATH as DEFAULT_DB_PATH
from .paths import database_path as database_path


def connect_rw(path: Path | None = None) -> sqlite3.Connection:
    """Open a writable connection with domain integrity checks enabled."""
    resolved = database_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(resolved, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


KEPT_BACKUP_GENERATIONS = 10
"""How many local checkpoints survive. Ten covers the migrations of several working
sessions, which is the window in which a migration defect is still being looked for;
past that the store has been read and written enough that a much older copy would be
restoring a different portfolio rather than repairing this one."""

_BACKUP_GLOB = "baibai-*.sqlite"


def initialize_database(
    path: Path | None = None,
    *,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> int:
    """Apply pending migrations, each as one immediate transaction.

    A checkpoint is taken first whenever there is anything to apply. The transaction
    around each migration only undoes statements that failed; a migration that runs
    exactly as written and means the wrong thing commits, and this store is the only
    copy of the judgments and the ledger. Routine writers call this on every command,
    so the point a defect is introduced is also the last point a copy can be taken —
    and the copy has to exist before the first statement rather than after it.
    """
    with closing(connect_rw(path)) as connection:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current > 0 and any(migration.version > current for migration in migrations):
            # Fails closed: a checkpoint that could not be written leaves the migration
            # unapplied, because the alternative is applying it with no way back. A store
            # still at version 0 is a file `connect_rw` has just created and holds nothing
            # a copy could give back.
            backup_database(path)
        for migration in migrations:
            if migration.version <= current:
                continue
            if migration.version != current + 1:
                raise RuntimeError(
                    f"migration sequence gap: database={current}, next={migration.version}"
                )
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in migration.statements:
                    connection.execute(statement)
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise sqlite3.IntegrityError(
                        f"foreign key check failed during migration {migration.version}"
                    )
                connection.execute(f"PRAGMA user_version = {migration.version}")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            current = migration.version
        return current


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
