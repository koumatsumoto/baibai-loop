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
