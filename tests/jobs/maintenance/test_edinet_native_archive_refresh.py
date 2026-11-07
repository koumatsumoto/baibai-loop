"""Manual facts/TOB retain exact native originals without narrowing annual scope."""

import io
from contextlib import closing
from unittest.mock import Mock

import pytest
from tests.helpers.edinet_metrics import _edinet_csv_zip
from tests.jobs.acquisition.test_edinet_archive_native_acquisition import AT, DAY, filing, setup

from baibai_engine.data_collection.providers.edinet.archive_native import (
    HEAD_KEY,
    EdinetArchiveL1,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.facts_scope import read_scope
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.edinet_archive_native import materialize_archives
from baibai_jobs.maintenance import edinet_extraction as refresh
from baibai_jobs.maintenance.source_refresh.edinet import EDINETRefresh


@pytest.mark.parametrize("tickers", [(), ("7203",)])
@pytest.mark.parametrize("l2_failure", [False, True])
def test_manual_facts_publishes_native_scope_before_http_and_keeps_it_after_l2_failure(
    tmp_path, monkeypatch, tickers, l2_failure
):
    owner, client, _, _ = setup(
        tmp_path,
        [
            filing("S100ONE"),
            filing("S100TWO", "67580"),
            filing("S100HOLD", docTypeCode="350", secCode=None),
        ],
    )
    owner.store.close = Mock()
    monkeypatch.setattr(refresh, "Boto3R2Store", lambda **_: owner.store)
    database = tmp_path / "absent.sqlite"
    response = client.fetch_archive.return_value
    expected = frozenset({"S100ONE"} if tickers else {"S100ONE", "S100TWO"})

    def fetch(doc_id, kind):
        assert owner.freeze().scope.doc_ids == expected
        assert not database.exists()  # No prior/current L2 is needed to retain the source.
        return response

    client.fetch_archive.side_effect = fetch
    extractor = Mock(return_value=int(l2_failure))
    monkeypatch.setattr(refresh, "extract_edinet_facts_command", extractor)
    assert refresh.refresh_edinet_facts_command(
        asof=DAY,
        sqlite_path=database,
        acquisition=client,
        initialize=True,
        tickers=tickers,
        stdout=io.StringIO(),
    ) == int(l2_failure)
    fixed = owner.freeze()
    assert fixed.scope.doc_ids == expected
    assert fixed.scope.since == (None if tickers else DAY)
    assert fixed.archive_keys == {(doc_id, 1) for doc_id in expected}
    assert not any(call.args[0] == "S100HOLD" for call in client.fetch_archive.call_args_list)
    with closing(open_connection(database)) as connection:
        assert read_scope(connection) == fixed.scope
    extractor.assert_called_once()


def test_unconfirmed_scope_never_changes_the_manual_sql_copy_or_starts_http(tmp_path, monkeypatch):
    owner, client, _, _ = setup(tmp_path, [filing("S100ONE")])
    owner.store.close = Mock()
    monkeypatch.setattr(refresh, "Boto3R2Store", lambda **_: owner.store)
    monkeypatch.setattr(owner.store, "put_file", Mock(side_effect=LakeCASConflict("injected")))
    database = tmp_path / "absent.sqlite"
    assert (
        refresh.refresh_edinet_facts_command(
            asof=DAY,
            sqlite_path=database,
            acquisition=client,
            initialize=True,
            stdout=io.StringIO(),
        )
        == 1
    )
    assert not database.exists()
    client.fetch_archive.assert_not_called()
    assert (tmp_path / "edinet-archive-work/pending.json").is_file()


def test_explicit_tob_zip_refresh_publishes_original_before_sql_and_reuses_it(
    tmp_path, monkeypatch
):
    owner, _, _, _ = setup(tmp_path, [])
    owner.store.close = Mock()
    monkeypatch.setattr(
        "baibai_jobs.maintenance.source_refresh.edinet.Boto3R2Store", lambda **_: owner.store
    )
    database = tmp_path / "market.sqlite"
    with closing(EDINETRefresh("synthetic", sqlite_path=database)) as client:
        http = Mock()
        http.fetch_archive.return_value = EdinetResponse(AT, _edinet_csv_zip())
        client._http = http
        retained = client._retain_document_zip

        def retain(doc_id, kind, content, *, stored_on):
            metadata = owner.freeze().metadata(doc_id, kind)
            assert metadata.received_at_utc == AT
            assert metadata.stored_on == stored_on
            return retained(doc_id, kind, content, stored_on=stored_on)

        monkeypatch.setattr(client, "_retain_document_zip", retain)
        assert client.download_csv_zip("S100OFFER") == http.fetch_archive.return_value.body
        owner.store.get_keys.clear()
        assert client.download_csv_zip("S100OFFER") == http.fetch_archive.return_value.body
        http.fetch_archive.assert_called_once_with("S100OFFER", 5)
        assert not any(key.endswith(".zip") for key in owner.store.get_keys)
        with closing(open_connection(database)) as connection, connection:
            connection.execute("DELETE FROM edinet_source_archives")
        assert client.download_csv_zip("S100OFFER") == http.fetch_archive.return_value.body
        http.fetch_archive.assert_called_once()
    assert (
        EdinetArchiveL1(owner.store, tmp_path / "read")
        .freeze()
        .metadata("S100OFFER", 5)
        .received_at_utc
        == AT
    )


def test_manual_zip_reads_freeze_metadata_once_and_still_refuse_pending_work(tmp_path, monkeypatch):
    owner, _, _, _ = setup(tmp_path, [])
    owner.store.close = Mock()
    monkeypatch.setattr(
        "baibai_jobs.maintenance.source_refresh.edinet.Boto3R2Store", lambda **_: owner.store
    )
    content = _edinet_csv_zip()
    documents = [f"S100WARM{number}" for number in range(5)]
    fixed = owner.publish(
        NativeArchive.from_response(doc_id, 5, EdinetResponse(AT, content)) for doc_id in documents
    )
    database = tmp_path / "market.sqlite"
    materialize_archives(fixed, database)
    owner.store.get_keys.clear()
    with closing(EDINETRefresh("synthetic", sqlite_path=database)) as client:
        http = Mock()
        client._http = http
        for doc_id in documents:
            assert client.download_csv_zip(doc_id) == content
        assert owner.store.get_keys == [HEAD_KEY]
        pending = tmp_path / "edinet-archive-work/pending.json"
        pending.parent.mkdir(exist_ok=True)
        pending.write_text("{}")
        with pytest.raises(LakePublishError, match="unpublished archives"):
            client.download_csv_zip(documents[0])
        assert owner.store.get_keys == [HEAD_KEY]
        http.fetch_archive.assert_not_called()
