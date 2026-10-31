import sqlite3
from contextlib import closing

import pytest

from baibai_engine.storage.sqlite.application.proposal_records_cutover import (
    migrate_proposal_records,
)
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_engine.storage.sqlite.application.write import initialize_database


def test_explicit_additive_cutover_preserves_old_rows_and_consistent_backup(tmp_path):
    path = tmp_path / "application.sqlite"
    backup = tmp_path / "before.sqlite"
    initialize_database(path)
    with closing(sqlite3.connect(path)) as c, c:
        for table in ["proposal_correction", "proposal_human_report"]:
            c.execute("DROP TABLE " + table)
        c.execute("PRAGMA user_version=27")
        c.execute(
            "INSERT INTO task VALUES ('fixture','open','other',NULL,'2026-10-14',NULL,'2026-10-14',NULL,'{}')"
        )
        before = c.execute("SELECT * FROM task").fetchall()
    with pytest.raises(RuntimeError, match="obsolete"):
        connect_read_only(path)
    migrate_proposal_records(path, backup)
    with closing(connect_read_only(path)) as c:
        assert c.execute("SELECT * FROM task").fetchall()[0][0] == "fixture"
        assert c.execute("PRAGMA user_version").fetchone()[0] == 28
        assert not c.execute("SELECT * FROM proposal_correction").fetchall()
        assert not c.execute("PRAGMA foreign_key_check").fetchall()
    with closing(sqlite3.connect(backup)) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 27
        assert c.execute("SELECT * FROM task").fetchall() == before
    with pytest.raises(ValueError, match="exactly schema v27"):
        migrate_proposal_records(path, tmp_path / "second.sqlite")
    assert not (tmp_path / "second.sqlite").exists()


def test_cutover_holds_writer_lock_and_rolls_back_failed_ddl(tmp_path, monkeypatch):
    from baibai_engine.storage.sqlite.application import proposal_records_cutover as module

    path = tmp_path / "application.sqlite"
    backup = tmp_path / "before.sqlite"
    initialize_database(path)
    with closing(sqlite3.connect(path)) as c, c:
        c.execute("DROP TABLE proposal_correction")
        c.execute("DROP TABLE proposal_human_report")
        c.execute("PRAGMA user_version=27")
    create = module.create_snapshot

    def snapshot_with_competing_writer(source, destination):
        with (
            closing(sqlite3.connect(source, timeout=0)) as c,
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            c.execute("BEGIN IMMEDIATE")
        create(source, destination)

    monkeypatch.setattr(module, "create_snapshot", snapshot_with_competing_writer)
    monkeypatch.setattr(
        module,
        "PROPOSAL_RECORDS_SCHEMA_SQL",
        module.PROPOSAL_RECORDS_SCHEMA_SQL + "\nINVALID DDL;\n",
    )
    with pytest.raises(sqlite3.OperationalError):
        module.migrate_proposal_records(path, backup)
    for source in [path, backup]:
        with closing(sqlite3.connect(source)) as c:
            assert c.execute("PRAGMA user_version").fetchone()[0] == 27
            assert not c.execute(
                "SELECT name FROM sqlite_schema WHERE name='proposal_correction'"
            ).fetchall()
    with pytest.raises(FileExistsError):
        module.migrate_proposal_records(path, backup)
