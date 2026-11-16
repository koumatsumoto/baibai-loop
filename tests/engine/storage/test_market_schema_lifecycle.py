import sqlite3
from contextlib import closing
from datetime import date

import pytest

from baibai_engine.machine_analysis.observations.financials.edinet_metrics.write import (
    store_edinet_metrics,
)
from baibai_engine.storage.sqlite.source_cache import schema


def test_connect_current_does_not_create_missing_file(tmp_path):
    path = tmp_path / "missing.sqlite"
    assert schema.connect_current(path) is None
    assert not path.exists()


@pytest.mark.parametrize("shape", ["missing", "zero_bytes", "sqlite_unwritten"])
def test_unwritten_market_can_be_initialized_by_normal_writer(tmp_path, shape):
    path = tmp_path / "market.sqlite"
    if shape == "zero_bytes":
        path.touch()
    elif shape == "sqlite_unwritten":
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("PRAGMA user_version = 0")
    assert (
        store_edinet_metrics(
            path, date(2026, 11, 14), [{"ticker": "7203", "extractor_revision": "test-revision"}]
        )
        == 1
    )
    with closing(schema.connect_current(path)) as connection:
        assert connection is not None
        schema.validate_current_schema(connection)
        assert connection.execute("SELECT ticker FROM edinet_metrics").fetchall() == [("7203",)]


@pytest.mark.parametrize("version", [51, schema.SQLITE_SCHEMA_VERSION, 53])
@pytest.mark.parametrize("opener", [schema.open_connection, schema.connect_current])
def test_versioned_empty_market_is_not_an_unwritten_database(tmp_path, version, opener):
    path = tmp_path / "market.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(f"PRAGMA user_version = {version}")
    before = path.read_bytes()
    with pytest.raises(schema.SQLiteSchemaError), closing(opener(path)):
        pass
    assert path.read_bytes() == before


def test_normal_writer_does_not_repair_a_versioned_empty_market(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(f"PRAGMA user_version = {schema.SQLITE_SCHEMA_VERSION}")
    before = path.read_bytes()
    with pytest.raises(schema.SQLiteSchemaError):
        store_edinet_metrics(
            path, date(2026, 11, 14), [{"ticker": "7203", "extractor_revision": "test-revision"}]
        )
    assert path.read_bytes() == before


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


def test_edinet_list_receipts_require_fresh_schema_and_index(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(schema.open_connection(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (
            schema.SQLITE_SCHEMA_VERSION,
        )
        assert [
            row[1] for row in connection.execute("PRAGMA table_info(edinet_document_lists)")
        ] == [
            "doc_date",
            "process_datetime",
            "result_count",
            "selected_received_at_utc",
            "response_json",
            "selected_receipt_id",
            "latest_received_at_utc",
            "rechecked_asof",
        ]
        assert [
            row[1] for row in connection.execute("PRAGMA table_info(edinet_list_responses)")
        ] == [
            "receipt_id",
            "doc_date",
            "received_at_utc",
            "body_gzip",
        ]
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_edinet_list_responses_date_received'"
        ).fetchone() == (1,)
        assert [
            row[1] for row in connection.execute("PRAGMA table_info(tradingview_source_receipts)")
        ] == [
            "receipt_id",
            "snapshot_date",
            "master_asof",
            "tool_name",
            "received_at_utc",
            "requested_symbols_json",
            "requested_columns_json",
            "result_json",
        ]


def test_version_only_change_cannot_pass_for_receipt_schema(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(schema.open_connection(path)) as connection, connection:
        connection.execute("DROP TABLE edinet_list_responses")
        connection.execute(f"PRAGMA user_version = {schema.SQLITE_SCHEMA_VERSION}")
    with pytest.raises(schema.SQLiteSchemaError, match="missing tables: edinet_list_responses"):
        schema.open_connection(path)


@pytest.mark.parametrize("version", [47, 50, 51])
@pytest.mark.parametrize("opener", [schema.open_connection, schema.connect_current])
def test_prior_market_schema_version_is_not_migrated_implicitly(tmp_path, version, opener):
    path = tmp_path / "market.sqlite"
    with closing(schema.open_connection(path)) as connection, connection:
        connection.execute(f"PRAGMA user_version = {version}")
    before = path.read_bytes()
    with pytest.raises(
        schema.SQLiteSchemaError,
        match=f"found user_version={version}, expected={schema.SQLITE_SCHEMA_VERSION}",
    ):
        opener(path)
    assert path.read_bytes() == before


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
@pytest.mark.parametrize(
    "failure", [RuntimeError, KeyboardInterrupt, schema.SQLiteSchemaError, sqlite3.OperationalError]
)
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


def test_schema_reuses_static_expectation_but_checks_database_changes(tmp_path, monkeypatch):
    path = tmp_path / "market.sqlite"
    with closing(schema.open_connection(path)) as connection:
        schema._expected_schema_shape.cache_clear()
        connect = sqlite3.connect
        derivations = []

        def track(database, *args, **kwargs):
            if database == ":memory:":
                derivations.append(database)
            return connect(database, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", track)
        schema.validate_current_schema(connection)
        schema.validate_current_schema(connection)
        assert derivations == [":memory:"]
        connection.execute("DROP TABLE jpx_earnings_calendar")
        with pytest.raises(schema.SQLiteSchemaError, match="missing tables: jpx_earnings_calendar"):
            schema.validate_current_schema(connection)
        assert derivations == [":memory:"]


@pytest.mark.parametrize("damage", ["version", "corrupt"])
def test_existing_unreadable_store_is_not_an_absent_cache(tmp_path, damage):
    path = tmp_path / "market.sqlite"
    if damage == "corrupt":
        path.write_bytes(b"not a SQLite database")
        error = sqlite3.DatabaseError
    else:
        with closing(schema.open_connection(path)) as connection, connection:
            connection.execute("PRAGMA user_version = 1")
        error = schema.SQLiteSchemaError
    before = path.read_bytes()
    with pytest.raises(error):
        schema.connect_current(path)
    assert path.read_bytes() == before
