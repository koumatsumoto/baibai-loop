import sqlite3
from contextlib import closing

import pytest

from baibai_engine.storage.observations import schema


def test_connect_current_does_not_create_missing_file(tmp_path):
    path = tmp_path / "missing.sqlite"
    assert schema.connect_current(path) is None
    assert not path.exists()


def test_failed_market_initialization_rolls_back_and_can_retry(tmp_path, monkeypatch):
    path = tmp_path / "market.sqlite"
    with monkeypatch.context() as patch:
        patch.setattr(schema, "_SCHEMA_SQL", schema._SCHEMA_SQL + "\nINVALID SQL;")
        with pytest.raises(sqlite3.Error):
            schema.open_connection(path)
    with closing(sqlite3.connect(path)) as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert tables == []
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    with closing(schema.open_connection(path)) as connection:
        schema.validate_current_schema(connection)
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0] == schema.SQLITE_SCHEMA_VERSION
        )


def test_connect_current_accepts_uri_sensitive_path_characters(tmp_path):
    path = tmp_path / "market #1?.sqlite"
    schema.open_connection(path).close()
    connection = schema.connect_current(path)
    assert connection is not None
    connection.close()


@pytest.mark.parametrize(
    ("extra_ddl", "message"),
    [
        ("CREATE TABLE contract_probe(value TEXT);", "missing tables: contract_probe"),
        (
            "ALTER TABLE jpx_earnings_calendar ADD COLUMN contract_probe TEXT;",
            "missing columns in jpx_earnings_calendar: contract_probe",
        ),
    ],
)
def test_schema_validation_derives_tables_and_columns_from_ddl(
    tmp_path, monkeypatch, extra_ddl, message
):
    path = tmp_path / "market.sqlite"
    schema.open_connection(path).close()
    before = path.read_bytes()
    monkeypatch.setattr(schema, "_SCHEMA_SQL", schema._SCHEMA_SQL + "\n" + extra_ddl)
    with (
        closing(sqlite3.connect(path)) as conn,
        pytest.raises(schema.SQLiteSchemaError, match=message),
    ):
        schema.validate_current_schema(conn)
    assert path.read_bytes() == before


@pytest.mark.parametrize("opener", [schema.open_connection, schema.connect_current])
@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_schema_validation_failure_closes_the_owned_connection(
    tmp_path, monkeypatch, opener, failure
):
    path = tmp_path / "market.sqlite"
    schema.open_connection(path).close()
    before = path.read_bytes()
    connect = sqlite3.connect
    opened = []

    def track(*args, **kwargs):
        connection = connect(*args, **kwargs)
        opened.append(connection)
        return connection

    def fail(connection):
        raise failure("interrupted validation")

    monkeypatch.setattr(sqlite3, "connect", track)
    monkeypatch.setattr(schema, "validate_current_schema", fail)
    with pytest.raises(failure, match="interrupted validation"):
        opener(path)
    assert opened
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    assert path.read_bytes() == before


def test_connect_current_cannot_write_to_the_market_store(tmp_path):
    path = tmp_path / "market.sqlite"
    schema.open_connection(path).close()
    before = path.read_bytes()
    connection = schema.connect_current(path)
    assert connection is not None
    with closing(connection) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == schema.SQLITE_SCHEMA_VERSION
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO jquants_market_calendar VALUES ('2026-10-15', 1)")
    assert path.read_bytes() == before
