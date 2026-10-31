"""Explicit v27→v28 additive cutover; runtime readers/writers never migrate."""

import sqlite3
from contextlib import closing
from pathlib import Path

from baibai_engine.storage.sqlite.application.schema import PROPOSAL_RECORDS_SCHEMA_SQL
from baibai_engine.storage.sqlite.application.write import connect_rw
from baibai_engine.storage.sqlite.snapshot import create_snapshot


def migrate_proposal_records(path: Path, backup_path: Path) -> None:
    if backup_path.exists():
        raise FileExistsError("migration backup already exists")
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as source:
        if source.execute("PRAGMA user_version").fetchone()[0] != 27:
            raise ValueError("proposal records cutover requires exactly schema v27")
    with closing(connect_rw(path)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("PRAGMA user_version").fetchone()[0] != 27:
                raise ValueError("application version changed during cutover")
            # Hold the writer lock while backing up through a separate read connection.
            # No other writer can advance the store between checkpoint and cutover.
            create_snapshot(path, backup_path)
            statement = ""
            for line in PROPOSAL_RECORDS_SCHEMA_SQL.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    connection.execute(statement)
                    statement = ""
            connection.execute("PRAGMA user_version=28")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise sqlite3.IntegrityError("cutover foreign key check failed")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
