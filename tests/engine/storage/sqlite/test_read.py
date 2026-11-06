import sqlite3
from contextlib import closing

import pytest

from baibai_engine.storage.sqlite import machine_analysis, read
from baibai_engine.storage.sqlite.application import read as application_read
from baibai_engine.storage.sqlite.application.write import initialize_database
from baibai_engine.storage.sqlite.source_cache import read as market_read
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

READERS = [
    read.connect_read_only,
    application_read.connect_read_only,
    market_read.connect_read_only,
    machine_analysis.connect_read_only,
]


@pytest.mark.parametrize("reader", READERS, ids=["generic", "application", "market", "runs"])
def test_reader_never_creates_a_missing_store(tmp_path, reader):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError, match="unable to open database"):
        reader(path)
    assert not path.exists()


@pytest.mark.parametrize("reader", READERS, ids=["generic", "application", "market", "runs"])
def test_reader_preserves_an_unwritten_store(tmp_path, reader):
    path = tmp_path / "empty #1?.sqlite"
    path.touch()
    before = set(tmp_path.iterdir())
    with closing(reader(path)) as connection:
        assert connection.row_factory is sqlite3.Row
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert read.written_schema_version(connection) is None
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE forbidden (value TEXT)")
    assert path.read_bytes() == b""
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize(
    ("version", "has_table", "expected"),
    [(0, False, None), (0, True, 0), (9, False, 9), (9, True, 9)],
)
def test_written_schema_version_distinguishes_unwritten_from_unversioned(
    tmp_path, version, has_table, expected
):
    path = tmp_path / "store.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        if has_table:
            connection.execute("CREATE TABLE example (value TEXT)")
        connection.execute(f"PRAGMA user_version = {version}")
    before = path.read_bytes()
    with closing(read.connect_read_only(path)) as connection:
        assert read.written_schema_version(connection) == expected
    assert read.is_unwritten_database(path) is (expected is None)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "reader",
    [
        application_read.connect_read_only,
        market_read.connect_read_only,
        machine_analysis.connect_read_only,
    ],
    ids=["application", "market", "runs"],
)
@pytest.mark.parametrize(("has_table", "version"), [(True, 0), (False, 999), (True, 999)])
def test_owner_reader_rejects_noncurrent_written_store(tmp_path, reader, has_table, version):
    path = tmp_path / "obsolete.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        if has_table:
            connection.execute("CREATE TABLE old_data (value TEXT)")
        connection.execute(f"PRAGMA user_version = {version}")
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="schema"):
        reader(path)
    assert path.read_bytes() == before


def test_owner_readers_keep_their_current_schema_and_clock(tmp_path):
    application = tmp_path / "application.sqlite"
    market = tmp_path / "market.sqlite"
    runs = tmp_path / "runs.sqlite"
    initialize_database(application)
    open_connection(market).close()
    machine_analysis.initialize_run_store(runs)

    with closing(application_read.connect_read_only(application)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM operation_session").fetchone()[0] == 0
    with closing(market_read.connect_read_only(market)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0] == 0
    with closing(machine_analysis.connect_read_only(runs)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_generation").fetchone()[0] == 0
        assert connection.execute(
            "SELECT run_clock(?)", ("2026-11-06T09:00:00.123456+09:00",)
        ).fetchone()[0] == ("2026-11-06T00:00:00.123456+00:00")


@pytest.mark.parametrize("reader", READERS, ids=["generic", "application", "market", "runs"])
@pytest.mark.parametrize("failure", [sqlite3.OperationalError, KeyboardInterrupt])
@pytest.mark.parametrize("statement", ["PRAGMA query_only = ON", "PRAGMA foreign_keys = ON"])
def test_failed_read_setup_closes_connection(tmp_path, monkeypatch, reader, failure, statement):
    path = tmp_path / "empty.sqlite"
    path.touch()
    connect = sqlite3.connect
    opened = []

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == statement:
                raise failure("interrupted setup")
            return super().execute(sql, parameters)

    def track(*args, **kwargs):
        connection = connect(*args, **kwargs, factory=FailingConnection)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", track)
    with pytest.raises(failure, match="interrupted setup"):
        reader(path)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")


@pytest.mark.parametrize("owner", [application_read, market_read, machine_analysis])
def test_interrupted_schema_check_closes_connection(tmp_path, monkeypatch, owner):
    path = tmp_path / "empty.sqlite"
    path.touch()
    opened = read.connect_read_only(path)

    def interrupt(connection):
        raise KeyboardInterrupt("interrupted schema check")

    monkeypatch.setattr(owner, "connect_sqlite_read_only", lambda path: opened)
    monkeypatch.setattr(owner, "written_schema_version", interrupt)
    with pytest.raises(KeyboardInterrupt, match="interrupted schema check"):
        owner.connect_read_only(path)
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened.execute("SELECT 1")
