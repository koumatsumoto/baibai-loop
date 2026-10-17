from contextlib import closing
from datetime import UTC, date, datetime

import pytest

from baibai_engine.collection.economic import acquisition as acquisition
from baibai_engine.collection.economic import store as store
from baibai_engine.collection.economic.documents import SourceDocument, read_documents
from baibai_engine.collection.economic.providers.base import ProviderFetchStoppedError
from baibai_engine.collection.economic.records import read_snapshots
from baibai_engine.collection.economic.sources import load_sources

DAY = date(2026, 7, 20)


def test_native_acquisition_is_source_scoped_and_does_not_require_a_numeric_reading(
    tmp_path, monkeypatch
):
    path = tmp_path / "source.sqlite"
    calls = []

    def fetch(source, **kwargs):
        calls.append(source.source_id)
        return {DAY: [{"row": {"RIFLGFCY10_N.B": "not a number", "extra": None}}]}

    monkeypatch.setattr(acquisition, "fetch_native_records", fetch)
    outcomes = acquisition.refresh_sources(["fed-h15", "fed-h15"], db_path=path, start=DAY, end=DAY)
    assert calls == ["fed-h15"]
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], acquisition.SourceRefreshSuccess)
    assert outcomes[0].record_count == 1
    with closing(store.open_read_only_connection(path)) as connection:
        rows = tuple(read_snapshots(connection, DAY, DAY, source_id="fed-h15"))
        assert rows[0].records[0]["row"] == {"RIFLGFCY10_N.B": "not a number", "extra": None}
        assert tuple(
            connection.execute(
                "SELECT source_id, status, record_count FROM economic_source_runs"
            ).fetchone()
        ) == ("fed-h15", "ok", 1)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert tables == {
            "economic_source_records",
            "economic_source_documents",
            "economic_source_runs",
        }


def test_uninterpreted_document_is_still_a_successful_source_receipt(tmp_path, monkeypatch):
    path = tmp_path / "source.sqlite"
    source = next(item for item in load_sources().values() if item.provider == "tsr_bankruptcies")
    document = SourceDocument(
        source.source_id,
        "history",
        source.source_url,
        "application/json",
        datetime(2026, 7, 20, tzinfo=UTC),
        b'{"unrecognized": null}',
        {},
    )
    monkeypatch.setattr(
        acquisition, "fetch_source_documents", lambda *args, **kwargs: iter([document])
    )
    outcome = acquisition.refresh_sources([source.source_id], db_path=path, start=DAY, end=DAY)[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    with closing(store.open_read_only_connection(path)) as connection:
        assert next(read_documents(connection, source.source_id)).payload == document.payload
        assert connection.execute("SELECT status FROM economic_source_runs").fetchone()[0] == "ok"


def test_failed_source_does_not_stop_another_source(tmp_path, monkeypatch):
    def fetch(source, **kwargs):
        if source.source_id == "fed-h15":
            raise RuntimeError("source response changed")
        return {DAY: [{"row": {"10年": "1.5"}}]}

    monkeypatch.setattr(acquisition, "fetch_native_records", fetch)
    outcomes = acquisition.refresh_sources(
        ["fed-h15", "mof-jgbcm-all"], db_path=tmp_path / "source.sqlite", start=DAY, end=DAY
    )
    assert isinstance(outcomes[0], acquisition.SourceRefreshFailure)
    assert isinstance(outcomes[1], acquisition.SourceRefreshSuccess)


def test_options_keep_received_days_when_a_later_day_fails(tmp_path, monkeypatch):
    class Source:
        def __init__(self, context):
            self.closed = False

        def fetch(self, day):
            if day != DAY:
                raise ProviderFetchStoppedError("request refused")
            return [{"uninterpreted": "raw option", "missing": None}]

        def close(self):
            self.closed = True

    monkeypatch.setattr(acquisition, "OptionChainSource", Source)
    path = tmp_path / "source.sqlite"
    outcome = acquisition.refresh_sources(
        ["jquants-options-n225"], db_path=path, start=DAY, end=date(2026, 7, 21)
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshFailure)
    with closing(store.open_read_only_connection(path)) as connection:
        assert (
            len(tuple(read_snapshots(connection, DAY, DAY, source_id="jquants-options-n225"))) == 1
        )
        assert tuple(
            connection.execute("SELECT status, record_count FROM economic_source_runs").fetchone()
        ) == ("failed", 1)


def test_unknown_only_request_does_not_create_a_database(tmp_path):
    path = tmp_path / "missing.sqlite"
    outcomes = acquisition.refresh_sources(["not-registered"], db_path=path, start=DAY, end=DAY)
    assert isinstance(outcomes[0], acquisition.SourceRefreshFailure)
    assert not path.exists()


def test_source_store_rejects_an_old_schema_without_modifying_it(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version = 11")
    before = path.read_bytes()
    with pytest.raises(store.EconomicSchemaError):
        store.open_connection(path)
    assert path.read_bytes() == before


def test_source_store_rejects_unexpected_trigger_without_running_it(tmp_path):
    path = tmp_path / "source.sqlite"
    with closing(store.initialize_database(path)) as connection:
        connection.execute(
            "INSERT INTO economic_source_records "
            "(source_id, observed_at, fetched_at, record_count, payload) "
            "VALUES ('fed-h15', '2026-07-20', '2026-07-20T00:00:00+00:00', 0, X'00')"
        )
        connection.execute(
            "CREATE TRIGGER erase_history BEFORE INSERT ON economic_source_records "
            "BEGIN DELETE FROM economic_source_records; END"
        )
        connection.commit()
        with pytest.raises(store.EconomicSchemaError, match="unexpected trigger"):
            store.validate_current_schema(connection)
        assert store.row_count(connection, "economic_source_records") == 1
    with pytest.raises(store.EconomicSchemaError, match="unexpected trigger"):
        store.open_connection(path)
