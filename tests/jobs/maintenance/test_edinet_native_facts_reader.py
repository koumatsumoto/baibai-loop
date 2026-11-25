"""Manual facts keeps an offline annual copy and extracts against the same open fixed head."""

import io
from contextlib import closing
from unittest.mock import Mock

import pytest
from tests.jobs.acquisition.test_edinet_archive_native_acquisition import AT, DAY, filing, setup

from baibai_engine.data_collection.providers.edinet import archive_native, list_native
from baibai_engine.data_collection.providers.edinet.archive_native import NativeArchive
from baibai_engine.data_collection.providers.edinet.facts_scope import FactsScope, read_scope
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.machine_analysis.observations.financials.edinet_reader import EDINETReader
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_jobs.acquisition import edinet_archive_native as acquisition
from baibai_jobs.integration import edinet_archive_native as archive_copy
from baibai_jobs.integration import edinet_list_native as list_copy
from baibai_jobs.maintenance import edinet_extraction as refresh


@pytest.mark.parametrize(
    ("initialize", "tickers", "report_status", "extract_exit"),
    [
        (False, (), "received", 0),
        (True, ("7203",), "failed", 0),
        (True, (), "received", 1),
        (False, ("7203",), "failed", 1),
    ],
)
def test_manual_facts_shares_post_acquisition_head_until_extraction_finishes(
    tmp_path, monkeypatch, initialize, tickers, report_status, extract_exit
):
    owner, client, _, lists = setup(tmp_path, [filing("S100ONE")])
    original = client.fetch_archive.return_value.body
    scope = FactsScope(doc_ids=frozenset({"S100ONE", "S100OLD", "S100MISSING"}))
    archives = owner.publish(
        [
            NativeArchive.from_response(doc_id, kind, EdinetResponse(AT, body))
            for doc_id, kind, body in (
                ("S100ONE", 1, original),
                ("S100OLD", 1, b"previously requested annual"),
                ("S100ONE", 5, b"same ID, different source"),
                ("S100HOLD", 1, b"holder"),
            )
        ],
        scope=scope,
    )
    owner.store.get_keys.clear()
    database = tmp_path / "market.sqlite"
    owner.store.close = Mock()
    events = []
    selected_owner = Mock()

    def acquire(*args, **kwargs):
        assert args == (selected_owner, client, lists)
        assert kwargs == {
            "group": "edinet-facts",
            "asof": DAY,
            "initialize_facts": initialize,
            "tickers": tickers,
        }
        events.append("acquire")
        return {"status": report_status}

    def freeze():
        assert events == ["acquire"]
        events.append("freeze")
        return archives

    def extract(**kwargs):
        assert events == ["acquire", "freeze"]
        owner.store.close.assert_not_called()
        list_materialize.assert_called_once_with(lists, database)
        with closing(connect_read_only(database)) as conn:
            assert read_scope(conn) == scope
            rows = conn.execute(
                "SELECT doc_id,source_type FROM edinet_source_archives ORDER BY doc_id"
            )
            assert [tuple(row) for row in rows] == [("S100OLD", 1), ("S100ONE", 1)]
        assert EDINETReader(database).read_xbrl_zip("S100ONE") == original
        assert EDINETReader(database).read_xbrl_zip("S100OLD") == b"previously requested annual"
        assert len([key for key in owner.store.get_keys if key.endswith(".zip")]) == 2
        assert kwargs == {
            "asof": DAY,
            "sqlite_path": database,
            "reader": archives,
            "initialize": initialize,
            "tickers": tickers,
            "stdout": out,
        }
        owner.store.close.assert_not_called()
        assert archives.read_xbrl_zip("S100ONE") == original
        events.append("extract")
        return extract_exit

    selected_owner.freeze.side_effect = freeze
    list_owner = Mock()
    list_owner.freeze.return_value = lists
    monkeypatch.setattr(refresh, "Boto3R2Store", lambda **_: owner.store)
    monkeypatch.setattr(archive_native, "EdinetArchiveL1", Mock(return_value=selected_owner))
    monkeypatch.setattr(list_native, "EdinetListL1", Mock(return_value=list_owner))
    monkeypatch.setattr(acquisition, "acquire_archives", acquire)
    list_materialize = Mock()
    monkeypatch.setattr(list_copy, "materialize_lists", list_materialize)
    monkeypatch.setattr(refresh, "extract_edinet_facts_command", extract)
    out = io.StringIO()
    result = refresh.refresh_edinet_facts_command(
        asof=DAY,
        sqlite_path=database,
        acquisition=client,
        initialize=initialize,
        tickers=tickers,
        stdout=out,
    )
    assert result == max(int(report_status == "failed"), extract_exit)
    assert events == ["acquire", "freeze", "extract"]
    selected_owner.freeze.assert_called_once_with()
    owner.store.close.assert_called_once_with()


@pytest.mark.parametrize("error_type", [LakePublishError, RuntimeError])
def test_manual_facts_closes_store_when_native_extraction_raises(tmp_path, monkeypatch, error_type):
    store = Mock()
    error = error_type("injected extraction failure")
    extractor = Mock(side_effect=error)
    archive_owner = Mock()
    monkeypatch.setattr(refresh, "Boto3R2Store", Mock(return_value=store))
    monkeypatch.setattr(archive_native, "EdinetArchiveL1", Mock(return_value=archive_owner))
    monkeypatch.setattr(list_native, "EdinetListL1", Mock())
    monkeypatch.setattr(acquisition, "acquire_archives", Mock(return_value={"status": "received"}))
    monkeypatch.setattr(archive_copy, "materialize_archives", Mock())
    monkeypatch.setattr(list_copy, "materialize_lists", Mock())
    monkeypatch.setattr(refresh, "extract_edinet_facts_command", extractor)
    args = {
        "asof": DAY,
        "sqlite_path": tmp_path / "unused.sqlite",
        "acquisition": Mock(),
        "stdout": io.StringIO(),
    }
    if error_type is LakePublishError:
        assert refresh.refresh_edinet_facts_command(**args) == 1
    else:
        with pytest.raises(RuntimeError) as raised:
            refresh.refresh_edinet_facts_command(**args)
        assert raised.value is error
    extractor.assert_called_once()
    assert extractor.call_args.kwargs["reader"] is archive_owner.freeze.return_value
    store.close.assert_called_once_with()
