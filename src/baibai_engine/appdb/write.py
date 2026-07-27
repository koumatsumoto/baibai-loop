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


def initialize_database(
    path: Path | None = None,
    *,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> int:
    """Apply pending migrations, each as one immediate transaction."""
    with closing(connect_rw(path)) as connection:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
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
) -> Path | None:
    """Create a consistent SQLite snapshot, including uncheckpointed WAL rows."""
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
    return target
