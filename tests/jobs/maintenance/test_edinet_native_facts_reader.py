"""Manual facts extraction reads the just-materialized head before closing its store."""

import io
import zipfile
from unittest.mock import Mock

import pytest
from tests.jobs.acquisition.test_edinet_archive_native_acquisition import AT, DAY, filing, setup

from baibai_engine.data_collection.providers.edinet import archive_native, list_native
from baibai_engine.data_collection.providers.edinet.archive_native import NativeArchive
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.acquisition import edinet_archive_native as acquisition
from baibai_jobs.integration import edinet_archive_native as archive_copy
from baibai_jobs.integration import edinet_list_native as list_copy
from baibai_jobs.maintenance import edinet_extraction as refresh


@pytest.mark.parametrize(("initialize", "tickers"), [(False, ()), (True, ("7203",))])
@pytest.mark.parametrize(
    ("report_status", "extract_exit"),
    [("received", 0), ("failed", 0), ("received", 1), ("failed", 1)],
)
def test_manual_facts_shares_post_acquisition_head_until_extraction_finishes(
    tmp_path, monkeypatch, initialize, tickers, report_status, extract_exit
):
    owner, client, _, lists = setup(tmp_path, [filing("S100ONE")])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("XBRL/PublicDoc/source.xbrl", "<xbrl/>")
    original = buffer.getvalue()
    archives = owner.publish(
        [NativeArchive.from_response("S100ONE", 1, EdinetResponse(AT, original))]
    )
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
            "include_holder_archives": False,
        }
        events.append("acquire")
        return {"status": report_status}

    def freeze():
        assert events == ["acquire"]
        events.append("freeze")
        return archives

    def materialize(selected, database):
        assert selected is archives
        assert database == tmp_path / "unused.sqlite"
        owner.store.close.assert_not_called()
        events.append("materialize")

    def extract(**kwargs):
        assert events == ["acquire", "freeze", "materialize"]
        list_materialize.assert_called_once_with(lists, tmp_path / "unused.sqlite")
        assert kwargs == {
            "asof": DAY,
            "sqlite_path": tmp_path / "unused.sqlite",
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
    monkeypatch.setenv("BAIBAI_EDINET_LIST_NATIVE", "true")
    monkeypatch.setattr(refresh, "Boto3R2Store", lambda **_: owner.store)
    monkeypatch.setattr(archive_native, "EdinetArchiveL1", Mock(return_value=selected_owner))
    monkeypatch.setattr(list_native, "EdinetListL1", Mock(return_value=list_owner))
    monkeypatch.setattr(acquisition, "acquire_archives", acquire)
    monkeypatch.setattr(archive_copy, "materialize_archives", materialize)
    list_materialize = Mock()
    monkeypatch.setattr(list_copy, "materialize_lists", list_materialize)
    monkeypatch.setattr(refresh, "extract_edinet_facts_command", extract)
    sql_reader = Mock()
    out = io.StringIO()
    result = refresh.refresh_edinet_facts_command(
        asof=DAY,
        sqlite_path=tmp_path / "unused.sqlite",
        reader=sql_reader,
        acquisition=client,
        initialize=initialize,
        tickers=tickers,
        stdout=out,
    )
    assert result == max(int(report_status == "failed"), extract_exit)
    assert events == ["acquire", "freeze", "materialize", "extract"]
    selected_owner.freeze.assert_called_once_with()
    sql_reader.read_xbrl_zip.assert_not_called()
    owner.store.close.assert_called_once_with()


@pytest.mark.parametrize("error_type", [LakePublishError, RuntimeError])
def test_manual_facts_closes_store_when_native_extraction_raises(tmp_path, monkeypatch, error_type):
    store = Mock()
    error = error_type("injected extraction failure")
    extractor = Mock(side_effect=error)
    archive_owner = Mock()
    monkeypatch.setenv("BAIBAI_EDINET_LIST_NATIVE", "true")
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
        "reader": Mock(),
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
