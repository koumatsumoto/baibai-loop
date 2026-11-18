"""既存Appへの追記接続は初期化を兼ねず、SQLiteの整合設定を維持する。"""

import sqlite3
from contextlib import closing

import pytest

from baibai_engine.storage.sqlite.application.schema import APPLICATION_SCHEMA_VERSION
from baibai_engine.storage.sqlite.application.write import connect_existing, initialize_database


@pytest.mark.parametrize("parent_exists", [False, True])
def test_missing_store_is_opened_with_noncreating_sqlite_mode(tmp_path, monkeypatch, parent_exists):
    path = tmp_path / "new" / "app #1?.sqlite"
    if parent_exists:
        path.parent.mkdir()
    before = set(tmp_path.rglob("*"))
    connect = sqlite3.connect
    calls = []

    def checked(database, *args, **kwargs):
        calls.append((database, kwargs))
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", checked)
    with pytest.raises(sqlite3.OperationalError, match="unable to open database"):
        connect_existing(path)
    assert calls == [(path.as_uri() + "?mode=rw", {"uri": True, "isolation_level": None})]
    assert set(tmp_path.rglob("*")) == before


def test_current_store_keeps_transaction_and_integrity_settings(tmp_path, monkeypatch):
    path = tmp_path / "app #1?.sqlite"
    initialize_database(path)
    before = path.read_bytes()
    monkeypatch.setenv("BAIBAI_DB", str(path))
    with closing(connect_existing()) as connection:
        assert connection.row_factory is sqlite3.Row
        assert connection.isolation_level is None
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 0
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE rolled_back(value TEXT)")
        connection.rollback()
        assert connection.execute("PRAGMA user_version").fetchone()[0] == APPLICATION_SCHEMA_VERSION
    assert path.read_bytes() == before


@pytest.mark.parametrize("state", ["unwritten", "unversioned", "unsupported", "not_sqlite"])
def test_uninitialized_or_unsupported_store_is_rejected_unchanged(tmp_path, monkeypatch, state):
    path = tmp_path / "app.sqlite"
    if state == "unwritten":
        path.touch()
    elif state == "not_sqlite":
        path.write_bytes(b"not a database")
    else:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE retained(value TEXT)")
            if state == "unsupported":
                connection.execute("PRAGMA user_version=999")
    before = path.read_bytes()
    entries_before = set(tmp_path.iterdir())
    connect = sqlite3.connect
    opened = []

    def track(*args, **kwargs):
        connection = connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", track)
    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        connect_existing(path)
    assert path.read_bytes() == before
    assert set(tmp_path.iterdir()) == entries_before
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")


@pytest.mark.parametrize("failure", [sqlite3.OperationalError, KeyboardInterrupt])
@pytest.mark.parametrize("statement", ["PRAGMA foreign_keys = ON", "PRAGMA user_version"])
def test_interrupted_append_setup_closes_connection(tmp_path, monkeypatch, failure, statement):
    path = tmp_path / "app.sqlite"
    initialize_database(path)
    before = path.read_bytes()
    connect = sqlite3.connect
    opened = []

    class InterruptedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == statement:
                raise failure("synthetic interrupted setup")
            return super().execute(sql, parameters)

    def track(*args, **kwargs):
        connection = connect(*args, **kwargs, factory=InterruptedConnection)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", track)
    with pytest.raises(failure, match="synthetic interrupted setup"):
        connect_existing(path)
    assert path.read_bytes() == before
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")
