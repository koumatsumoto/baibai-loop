"""Manual metrics plans from SQL and reads only needed bytes from its acquired head."""

import io
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.helpers.edinet_metrics import _edinet_csv_zip
from tests.jobs.acquisition.test_edinet_archive_native_acquisition import AT, DAY, setup

from baibai_engine.data_collection.providers.edinet import list_native
from baibai_engine.data_collection.providers.edinet.archive_native import NativeArchive
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.integration import edinet_list_native as list_copy
from baibai_jobs.maintenance import edinet_extraction as refresh
from baibai_jobs.maintenance.source_refresh import edinet as source


def test_retained_archives_never_initializes_or_reopens_a_source(tmp_path, monkeypatch):
    factory = Mock(side_effect=AssertionError("store created by reader getter"))
    monkeypatch.setattr(source, "Boto3R2Store", factory)
    with closing(
        source.EDINETRefresh("synthetic", sqlite_path=tmp_path / "unused.sqlite")
    ) as client:
        with pytest.raises(ValueError, match="not been selected"):
            client.retained_archives()
        assert client._http is None
    with pytest.raises(ValueError, match="not been selected"):
        client.retained_archives()
    factory.assert_not_called()


def test_retained_archives_keeps_self_publication_but_does_not_resolve_new_heads(
    tmp_path, monkeypatch
):
    owner, _, _, _ = setup(tmp_path, [])
    owner.store.close = Mock()
    monkeypatch.setattr(source, "Boto3R2Store", Mock(return_value=owner.store))
    content = _edinet_csv_zip()
    with closing(
        source.EDINETRefresh("synthetic", sqlite_path=tmp_path / "unused.sqlite")
    ) as client:
        http = Mock()
        http.fetch_archive.return_value = EdinetResponse(AT, content)
        client._http = http
        monkeypatch.setattr(client, "_retain_document_zip", lambda _doc, _kind, body, **_: body)
        client.download_csv_zip("S100FIRST")
        first = client.retained_archives()
        client.download_csv_zip("S100SECOND")
        second = client.retained_archives()
        assert first is not second
        assert first.archive_keys == {("S100FIRST", 5)}
        assert second.archive_keys == {("S100FIRST", 5), ("S100SECOND", 5)}
        owner.publish([NativeArchive.from_response("S100LATER", 5, EdinetResponse(AT, content))])
        before = list(owner.store.get_keys)
        assert client.retained_archives() is second
        assert owner.store.get_keys == before
        assert ("S100LATER", 5) not in second.archive_keys
        assert second.read_csv_zip("S100FIRST") == content
    with pytest.raises(ValueError, match="not been selected"):
        client.retained_archives()
    owner.store.close.assert_called_once_with()
    http.close.assert_called_once_with()


@pytest.mark.parametrize(
    ("needs_body", "source_ids"),
    [(True, ("S100ONE",)), (False, ("S100ONE",)), (False, ())],
)
def test_refresh_acquires_before_sql_plan_and_only_selects_a_body_reader_when_needed(
    tmp_path, monkeypatch, needs_body, source_ids
):
    _wire_refresh(tmp_path, monkeypatch, needs_body=needs_body, source_ids=source_ids)


def _wire_refresh(
    tmp_path,
    monkeypatch,
    *,
    needs_body=True,
    source_ids=("S100ONE",),
    error=None,
    invalid_plan=False,
):
    events = []
    sql_reader = Mock()
    native_reader = Mock()
    client = Mock()
    client.list_documents.side_effect = lambda day: events.append(("list", day))
    client.download_csv_zip.side_effect = lambda doc: events.append(("acquire", doc))
    client.retained_archives.side_effect = lambda: events.append(("reader",)) or native_reader
    plan = SimpleNamespace(archive_candidates=(object(),) if needs_body else ())
    store = Mock()
    monkeypatch.setattr(refresh, "Boto3R2Store", Mock(return_value=store))
    monkeypatch.setattr(list_native, "EdinetListL1", Mock())
    monkeypatch.setattr(list_copy, "materialize_lists", Mock(return_value={}))
    monkeypatch.setattr(refresh, "metric_archive_candidates", Mock(return_value=(source_ids, None)))
    monkeypatch.setattr(refresh, "_require_metric_document_window", Mock())

    def prepare(**kwargs):
        assert kwargs["reader"] is sql_reader
        assert kwargs["asof_date"] == DAY
        assert kwargs["lookback_days"] == 0
        assert [item[1] for item in events if item[0] == "acquire"] == list(source_ids)
        events.append(("plan",))
        return None if invalid_plan else plan

    def extract(**kwargs):
        assert kwargs["plan"] is plan
        assert kwargs["reader"] is (native_reader if needs_body else sql_reader)
        if needs_body:
            assert events[-1] == ("reader",)
        if error is not None:
            raise error
        return 0

    planner = Mock(side_effect=prepare)
    extractor = Mock(side_effect=extract)
    monkeypatch.setattr(refresh, "prepare_edinet_metric_plan", planner)
    monkeypatch.setattr(refresh, "extract_edinet_metrics_from_plan", extractor)
    result = refresh.refresh_edinet_metrics_command(
        asof_date=DAY,
        lookback_days=0,
        reader=sql_reader,
        acquisition=client,
        sqlite_path=tmp_path / "unused.sqlite",
        stdout=io.StringIO(),
    )
    assert result == int(invalid_plan)
    planner.assert_called_once()
    assert client.retained_archives.call_count == int(needs_body and not invalid_plan)
    if invalid_plan:
        extractor.assert_not_called()
    else:
        extractor.assert_called_once()
    client.list_documents.assert_not_called()
    store.close.assert_called_once_with()
    return client


def test_failed_sql_plan_does_not_select_a_native_body_reader(tmp_path, monkeypatch):
    _wire_refresh(tmp_path, monkeypatch, invalid_plan=True)


def test_native_body_failure_is_not_replaced_by_the_sql_reader(tmp_path, monkeypatch):
    failure = LakePublishError("native body changed")
    with pytest.raises(LakePublishError) as raised:
        _wire_refresh(tmp_path, monkeypatch, error=failure)
    assert raised.value is failure
