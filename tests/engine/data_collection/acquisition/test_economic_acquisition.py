import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

import baibai_engine.data_collection.acquisition.refresh as acquisition
from baibai_engine.data_collection.catalog import load_sources
from baibai_engine.data_collection.receipts.native_sources import store as store
from baibai_engine.data_collection.receipts.native_sources.documents import (
    SourceDocument,
    read_documents,
    store_document,
)
from baibai_engine.data_collection.receipts.native_sources.records import read_snapshots
from baibai_engine.data_collection.transport.context import FetchContext, ProviderFetchStoppedError

DAY = date(2026, 7, 20)


@pytest.mark.parametrize(
    "provider", ["spglobal_pmi", "tradingeconomics_pmi", "tsr_bankruptcies", "matsui_margin"]
)
def test_document_refresh_passes_receipt_scope_without_loading_old_bodies(
    tmp_path, monkeypatch, provider
):
    source = replace(load_sources()["spglobal-jp-pmi-mfg"], provider=provider)
    old = SourceDocument(
        source_id=source.source_id,
        request_key="2026-07-01",
        source_url="https://example.com/old.pdf",
        content_type="application/pdf",
        fetched_at=datetime(2026, 7, 20, tzinfo=UTC),
        payload=b"old body",
        metadata={"observed_at": "2026-07-01", "phase": "final"},
    )
    latest = replace(
        old,
        source_url="https://example.com/latest.html",
        content_type="text/html",
        fetched_at=datetime(2026, 7, 21, tzinfo=UTC),
        payload=b"new body",
        metadata={"observed_at": "2026-07-01", "phase": "flash"},
    )
    inspected = []

    def fetch(definition, *, existing, **kwargs):
        assert definition == source
        assert list(existing) == [latest.request_key]
        stored = existing[latest.request_key]
        for field in (
            "source_id",
            "request_key",
            "source_url",
            "content_type",
            "fetched_at",
            "metadata",
        ):
            assert getattr(stored, field) == getattr(latest, field)
        assert not hasattr(stored, "payload")
        inspected.append(stored)
        return ()

    monkeypatch.setattr(acquisition, "fetch_source_documents", fetch)
    with (
        closing(store.initialize_database(tmp_path / "source.sqlite")) as conn,
        FetchContext() as ctx,
    ):
        with conn:
            store_document(conn, old)
            store_document(conn, latest)
            store_document(conn, replace(old, source_id="other-source"))
        conn.set_authorizer(
            lambda action, table, column, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ
                and table == "economic_source_documents"
                and column == "payload"
                else sqlite3.SQLITE_OK
            )
        )
        assert acquisition._refresh_source(conn, source, start=DAY, end=DAY, context=ctx) == 0
        assert len(inspected) == 1


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
            "economic_source_record_confirmations",
            "economic_source_collection_confirmations",
        }


def test_uninterpreted_document_is_still_a_successful_source_receipt(tmp_path, monkeypatch):
    path = tmp_path / "source.sqlite"
    source = next(item for item in load_sources().values() if item.provider == "tsr_bankruptcies")
    document = SourceDocument(
        source_id=source.source_id,
        request_key="history",
        source_url=source.source_url,
        content_type="application/json",
        fetched_at=datetime(2026, 7, 20, tzinfo=UTC),
        payload=b'{"unrecognized": null}',
        metadata={},
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


def test_old_pdf_failure_keeps_original_and_does_not_block_new_month(tmp_path, monkeypatch):
    import baibai_engine.data_collection.providers.spglobal.pmi as pmi
    from baibai_engine.data_collection.receipts.native_sources.documents import store_document
    from baibai_engine.data_collection.transport.context import IndicatorsProviderError

    source_id = "spglobal-jp-pmi-mfg"
    old, new = date(2026, 7, 1), date(2026, 9, 1)
    old_url, new_url = "https://example.com/old.pdf", "https://example.com/new.pdf"
    monkeypatch.setattr(
        pmi,
        "load_manifest",
        lambda _: {
            "jp_manufacturing": (
                pmi.Release(old, old, old_url),
                pmi.Release(new, new, new_url),
            )
        },
    )
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        if url == old_url:
            raise IndicatorsProviderError("404: expired PDF URL")
        return b"%PDF newer original"

    monkeypatch.setattr(pmi, "fetch_pdf_bytes", fetch)
    path = tmp_path / "source.sqlite"
    with closing(store.initialize_database(path)) as conn, conn:
        store_document(
            conn,
            SourceDocument(
                source_id=source_id,
                request_key=old.isoformat(),
                source_url=old_url,
                content_type="application/pdf",
                fetched_at=datetime(2026, 8, 1, tzinfo=UTC),
                payload=b"%PDF retained old original",
                metadata={"observed_at": old.isoformat(), "release_observed_at": old.isoformat()},
            ),
        )
    outcome = acquisition.refresh_sources(
        [source_id], db_path=path, start=old, end=date(2026, 10, 23)
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshFailure)
    assert calls == [old_url, new_url]
    with closing(store.open_read_only_connection(path)) as conn:
        documents = {item.request_key: item for item in read_documents(conn, source_id)}
        assert documents[old.isoformat()].payload == b"%PDF retained old original"
        assert documents[new.isoformat()].payload == b"%PDF newer original"
        assert tuple(
            conn.execute("SELECT status, record_count FROM economic_source_runs").fetchone()
        ) == ("failed", 1)


def test_pmi_rate_limit_stops_new_requests_to_sibling_series(tmp_path, monkeypatch):
    import baibai_engine.data_collection.providers.spglobal.pmi as pmi
    from baibai_engine.data_collection.transport.context import ProviderRateLimitError

    sources = [s for s in load_sources().values() if s.provider == "spglobal_pmi"]
    month = date(2026, 9, 1)
    release = pmi.Release(month, month, "https://example.com/release.pdf")
    monkeypatch.setattr(
        pmi, "load_manifest", lambda _: {s.provider_series_id: (release,) for s in sources}
    )
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        raise ProviderRateLimitError("429")

    monkeypatch.setattr(pmi, "fetch_pdf_bytes", fetch)
    outcomes = acquisition.refresh_sources(
        [s.source_id for s in sources],
        db_path=tmp_path / "source.sqlite",
        start=month,
        end=date(2026, 10, 23),
    )
    assert len(calls) == 1
    assert all(isinstance(item, acquisition.SourceRefreshFailure) for item in outcomes)


def test_pmi_refusal_stops_this_route_without_changing_other_provider_fallbacks(
    tmp_path, monkeypatch
):
    import baibai_engine.data_collection.providers.spglobal.pmi as pmi
    from baibai_engine.data_collection.transport.context import SourceWithheldError

    sources = [s for s in load_sources().values() if s.provider == "spglobal_pmi"]
    month = date(2026, 9, 1)
    release = pmi.Release(month, month, "https://example.com/release.pdf")
    monkeypatch.setattr(
        pmi, "load_manifest", lambda _: {s.provider_series_id: (release,) for s in sources}
    )
    calls = []

    def refused(url, **kwargs):
        calls.append(url)
        raise SourceWithheldError("HTTP 403")

    monkeypatch.setattr(pmi, "fetch_pdf_bytes", refused)
    monkeypatch.setattr(acquisition, "fetch_native_records", lambda *a, **k: {})
    selected = [s.source_id for s in sources] + ["mof-jgbcm-all"]
    outcomes = acquisition.refresh_sources(
        selected, db_path=tmp_path / "source.sqlite", start=month, end=date(2026, 10, 23)
    )
    assert len(calls) == 1
    assert all(isinstance(item, acquisition.SourceRefreshFailure) for item in outcomes[:-1])
    assert isinstance(outcomes[-1], acquisition.SourceRefreshSuccess)


def test_pmi_missing_middle_manifest_month_is_not_complete_coverage(tmp_path, monkeypatch):
    import baibai_engine.data_collection.providers.spglobal.pmi as pmi

    source_id = "spglobal-jp-pmi-mfg"
    months = (date(2026, 7, 1), date(2026, 9, 1))
    releases = (
        pmi.Release(months[0], months[0], "https://example.com/first.pdf"),
        pmi.Release(months[1], months[1], "https://example.com/last.pdf"),
    )
    monkeypatch.setattr(pmi, "load_manifest", lambda _: {"jp_manufacturing": releases})
    monkeypatch.setattr(pmi, "fetch_pdf_bytes", lambda *a, **k: b"%PDF original")
    path = tmp_path / "source.sqlite"
    outcome = acquisition.refresh_sources(
        [source_id], db_path=path, start=months[0], end=date(2026, 10, 23)
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshFailure)
    assert "2026-08-01" in outcome.message
    with closing(store.open_read_only_connection(path)) as conn:
        assert len(tuple(read_documents(conn, source_id))) == 2


def test_shared_store_failure_stops_before_fetching_another_source(tmp_path, monkeypatch):
    fetched = []

    def fetch(source, **kwargs):
        fetched.append(source.source_id)
        return {DAY: [{"raw": "retained only if storage succeeds"}]}

    def broken_store(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(acquisition, "fetch_native_records", fetch)
    monkeypatch.setattr(acquisition, "store_snapshot", broken_store)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        acquisition.refresh_sources(
            ["fed-h15", "mof-jgbcm-all"],
            db_path=tmp_path / "source.sqlite",
            start=DAY,
            end=DAY,
        )
    assert fetched == ["fed-h15"]
