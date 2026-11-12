"""ZIP GETは固定L1一覧とscopeだけで計画し、1件ずつ原本を確定する。"""

import io
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock
from uuid import UUID

import pytest
from tests.helpers.edinet_metrics import _edinet_csv_zip, _edinet_document
from tests.helpers.edinet_research_sources import source_zip
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.edinet.archive_native import (
    EdinetArchiveL1,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.facts_scope import FactsScope, merge_scope
from baibai_engine.data_collection.providers.edinet.list_native import (
    EdinetListL1,
    NativeListReceipt,
)
from baibai_engine.data_collection.providers.edinet.models import (
    EdinetAccessDeniedError,
    EdinetAcquisitionError,
    EdinetRateLimitError,
    EdinetResponse,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.service import (
    extract_edinet_facts_command,
)
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.edinet_archive_native import acquire_archives
from baibai_jobs.integration.edinet_list_native import materialize_lists

DAY = date(2026, 11, 5)
AT = datetime(2026, 11, 5, 12, 34, 56, tzinfo=UTC)


def filing(doc_id, ticker="72030", **extra):
    return {
        **_edinet_document(ticker=ticker, doc_id=doc_id),
        "docInfoEditStatus": "0",
        **extra,
    }


def save_lists(owner, documents, *, day=DAY, receipt=1):
    body = json.dumps(
        {
            "metadata": {"resultset": {"count": len(documents)}},
            "results": [{"seqNumber": n, **doc} for n, doc in enumerate(documents, 1)],
        }
    ).encode()
    return owner.publish(
        [
            NativeListReceipt.from_response(
                day, EdinetResponse(AT + timedelta(seconds=receipt), body), UUID(int=receipt).hex
            )
        ]
    )


def setup(tmp_path, documents):
    store = MemoryR2Store()
    lists = EdinetListL1(store, tmp_path / "lists")
    lists.freeze(require_existing=False)
    fixed = save_lists(lists, documents)
    owner = EdinetArchiveL1(store, tmp_path / "archives")
    owner.initialize()
    client = Mock()
    client.fetch_archive.return_value = EdinetResponse(AT, source_zip())
    return owner, client, lists, fixed


def test_metrics_gets_ignore_l2_and_do_not_retrieve_retained_zip_bodies(tmp_path):
    owner, client, _, lists = setup(tmp_path, [filing("S100ONE")])
    client.fetch_archive.return_value = EdinetResponse(AT, _edinet_csv_zip())
    report = acquire_archives(
        owner, client, lists, group="edinet-metrics", asof=DAY, lookback_days=0
    )
    assert report["status"] == "received"
    assert report["downloaded"] == 1
    client.fetch_archive.assert_called_once_with("S100ONE", 5)
    saved = owner.freeze()
    assert saved.metadata("S100ONE", 5).received_at_utc == AT
    body_keys = {key for key in owner.store.values if key.endswith(".zip")}
    assert len(body_keys) == 1
    owner.store.get_keys.clear()
    client.reset_mock()
    repeated = acquire_archives(
        owner, client, lists, group="edinet-metrics", asof=DAY, lookback_days=0
    )
    client.fetch_archive.assert_not_called()
    assert repeated["retained_archives_reused"] == 1
    assert repeated["l1_identity"] == saved.identity
    assert not body_keys.intersection(owner.store.get_keys)
    saved_input = json.loads((owner.directory / "input.json").read_bytes())
    assert saved_input["archive_head"] == saved.value()
    assert saved_input["list_head"] == lists.value()
    assert not list(owner.directory.glob("*.sqlite"))


@pytest.mark.parametrize("lookback", [1, 540])
@pytest.mark.parametrize("missing_kind", ["unreceived", "failed"])
def test_missing_list_days_do_not_block_known_metric_originals_or_complete_l2(
    tmp_path, lookback, missing_kind
):
    from baibai_engine.machine_analysis.observations.financials.edinet_integration import (
        extract_saved_edinet,
    )

    owner, client, list_owner, lists = setup(tmp_path, [filing("S100ONE")])
    client.fetch_archive.return_value = EdinetResponse(AT, _edinet_csv_zip())
    failed_day = DAY - timedelta(days=1)
    if missing_kind == "failed":
        lists = list_owner.publish(
            [
                NativeListReceipt.from_response(
                    failed_day, EdinetResponse(AT + timedelta(seconds=2), b"{"), UUID(int=2).hex
                )
            ]
        )
    report = acquire_archives(
        owner, client, lists, group="edinet-metrics", asof=DAY, lookback_days=lookback
    )
    assert report["status"] == "failed"
    assert report["list_unavailable_days"] == [
        (DAY - timedelta(days=offset)).isoformat() for offset in range(lookback, 0, -1)
    ]
    assert report["list_failed_days"] == (
        [failed_day.isoformat()] if missing_kind == "failed" else []
    )
    assert report["downloaded"] == 1
    client.fetch_archive.assert_called_once_with("S100ONE", 5)
    fixed = owner.freeze()
    original = fixed.metadata("S100ONE", 5)
    assert original.received_at_utc == AT
    database = tmp_path / "market.sqlite"
    materialize_lists(lists, database)
    result = extract_saved_edinet(
        database, asof=DAY, lookback_days=lookback, metric_archives=fixed, fact_archives=fixed
    )
    assert result["edinet-metrics"]["status"] == "deferred_input"
    assert result["edinet-metrics"]["snapshot_asof"] is None
    assert result["edinet-metrics"]["deferred_inputs"] == ["document_window_incomplete"]

    # Once the missing list arrives, the same fixed-window acquisition reuses the
    # already received ZIP and clock. List repair does not authorize a replacement GET.
    if lookback == 1:
        complete_lists = save_lists(list_owner, [], day=failed_day, receipt=3)
        client.reset_mock()
        resumed = acquire_archives(
            owner, client, complete_lists, group="edinet-metrics", asof=DAY, lookback_days=1
        )
        assert resumed["status"] == "received"
        assert resumed["list_unavailable_days"] == []
        assert resumed["retained_archives_reused"] == 1
        assert owner.freeze().metadata("S100ONE", 5) == original
        assert owner.freeze().identity == fixed.identity
        client.fetch_archive.assert_not_called()


def test_scope_precedes_get_partial_429_and_resume_preserve_success(tmp_path):
    owner, client, _, lists = setup(tmp_path, [filing("S100ONE"), filing("S100TWO", "67580")])
    responses = iter([EdinetResponse(AT, source_zip()), EdinetRateLimitError("limited")])

    def fetch(doc_id, source_type):
        assert owner.freeze().scope == FactsScope(doc_ids=frozenset({"S100ONE", "S100TWO"}))
        result = next(responses)
        if isinstance(result, Exception):
            assert owner.freeze().archive_keys == frozenset({("S100ONE", 1)})
            raise result
        return result

    client.fetch_archive.side_effect = fetch
    report = acquire_archives(
        owner, client, lists, group="edinet-facts", asof=DAY, initialize_facts=True
    )
    assert report["status"] == "failed"
    assert report["downloaded"] == 1
    assert owner.freeze().scope.since is None
    first = owner.freeze().metadata("S100ONE", 1)
    client.reset_mock()
    client.fetch_archive.side_effect = None
    client.fetch_archive.return_value = EdinetResponse(AT + timedelta(days=1), source_zip())
    repeated = acquire_archives(
        owner, client, lists, group="edinet-facts", asof=DAY, initialize_facts=True
    )
    assert repeated["status"] == "received"
    client.fetch_archive.assert_called_once_with("S100TWO", 1)
    assert owner.freeze().metadata("S100ONE", 1) == first
    assert owner.freeze().scope.since == DAY


@pytest.mark.parametrize(
    ("documents", "tickers", "enabled", "gets"),
    [
        ([filing("S100ONE")], (), True, 1),
        ([filing("S100ONE")], ("7203",), False, 1),
        ([], (), False, 0),
        ([filing("S100ONE", xbrlFlag="0")], (), True, 0),
        ([filing("S100ONE", submitDateTime="2026-11-06 12:00")], (), True, 0),
    ],
)
def test_explicit_initialization_preserves_existing_scope_semantics(
    tmp_path, documents, tickers, enabled, gets
):
    owner, client, _, lists = setup(tmp_path, documents)
    report = acquire_archives(
        owner, client, lists, group="edinet-facts", asof=DAY, initialize_facts=True, tickers=tickers
    )
    assert report["status"] == "received"
    assert client.fetch_archive.call_count == gets
    # 現行initializeの選択scope条件を保つ。未公表・未取得の抽出成功を意味しない。
    assert owner.freeze().scope.since == (DAY if enabled else None)
    assert owner.freeze().scope.doc_ids == frozenset(doc["docID"] for doc in documents)


def test_invalid_zip_keeps_requested_scope_without_global_enable(tmp_path):
    owner, client, _, lists = setup(tmp_path, [filing("S100ONE")])
    client.fetch_archive.return_value = EdinetResponse(AT, b"not zip")
    report = acquire_archives(
        owner, client, lists, group="edinet-facts", asof=DAY, initialize_facts=True
    )
    assert report["status"] == "failed"
    assert owner.freeze().scope == FactsScope(doc_ids=frozenset({"S100ONE"}))
    assert not owner.freeze().archive_keys


def test_uninitialized_annual_still_gets_holder_batch_without_all_zip_hydration(
    tmp_path, monkeypatch
):
    documents = [filing(f"S100H{n:03d}", docTypeCode="350", secCode=None) for n in range(102)]
    owner, client, _, lists = setup(tmp_path, [filing("S100ANNUAL"), *documents])
    content = source_zip()
    client.fetch_archive.side_effect = lambda doc_id, _: EdinetResponse(
        AT, content + doc_id.encode()
    )
    sleeps = []
    monkeypatch.setattr("baibai_jobs.acquisition.edinet_archive_native.time.sleep", sleeps.append)
    report = acquire_archives(owner, client, lists, group="edinet-facts", asof=DAY)
    assert report["annual_archive_plan"] == "skipped_not_initialized"
    assert report["downloaded"] == 100
    assert report["holder_not_selected_this_batch"] == 2
    assert client.fetch_archive.call_args_list[0].args == ("S100H101", 1)
    assert client.fetch_archive.call_args_list[-1].args == ("S100H002", 1)
    assert sleeps == [10] * 100
    assert owner.freeze().scope == FactsScope()
    old_bodies = {key for key in owner.store.values if key.endswith(".zip")}
    owner.store.get_keys.clear()
    client.reset_mock()
    repeated = acquire_archives(owner, client, lists, group="edinet-facts", asof=DAY)
    assert repeated["downloaded"] == 2
    assert [call.args for call in client.fetch_archive.call_args_list] == [
        ("S100H001", 1),
        ("S100H000", 1),
    ]
    assert not old_bodies.intersection(owner.store.get_keys)


def test_holder_planning_failure_does_not_discard_annual_originals(tmp_path, monkeypatch):
    owner, client, _, lists = setup(tmp_path, [filing("S100ONE")])
    monkeypatch.setattr(
        "baibai_jobs.acquisition.edinet_archive_native.select_holder_archives",
        Mock(side_effect=ValueError("bad holder")),
    )
    report = acquire_archives(
        owner, client, lists, group="edinet-facts", asof=DAY, initialize_facts=True
    )
    assert report["status"] == "failed"
    assert report["holder_planning_failed"] is True
    assert owner.freeze().archive_keys == frozenset({("S100ONE", 1)})
    assert owner.freeze().scope.since == DAY


@pytest.mark.parametrize("status", [401, 403])
def test_holder_auth_failure_stops_without_global_annual_scope(tmp_path, monkeypatch, status):
    owner, client, _, lists = setup(
        tmp_path,
        [
            filing("S100ONE", docTypeCode="350", secCode=None),
            filing("S100TWO", docTypeCode="350", secCode=None),
        ],
    )
    monkeypatch.setattr("baibai_jobs.acquisition.edinet_archive_native.time.sleep", Mock())
    client.fetch_archive.side_effect = EdinetAccessDeniedError(status)
    report = acquire_archives(owner, client, lists, group="edinet-facts", asof=DAY)
    assert report["status"] == "failed"
    assert client.fetch_archive.call_count == 1
    assert owner.freeze().scope == FactsScope()


def test_saved_list_binding_does_not_advance_during_get(tmp_path):
    owner, client, list_owner, fixed = setup(tmp_path, [filing("S100ONE")])

    def fetch(*args):
        save_lists(list_owner, [filing("S100TWO")], receipt=2)
        return EdinetResponse(AT, _edinet_csv_zip())

    client.fetch_archive.side_effect = fetch
    report = acquire_archives(
        owner, client, fixed, group="edinet-metrics", asof=DAY, lookback_days=0
    )
    client.fetch_archive.assert_called_once_with("S100ONE", 5)
    assert report["list_identity"] == fixed.identity
    assert report["list_identity"] != list_owner.freeze().identity


def test_malformed_latest_list_is_reported_while_retaining_usable_prior_archive(tmp_path):
    owner, client, list_owner, _ = setup(tmp_path, [filing("S100ONE")])
    fixed = list_owner.publish(
        [
            NativeListReceipt.from_response(
                DAY, EdinetResponse(AT + timedelta(seconds=2), b"{"), UUID(int=2).hex
            )
        ]
    )
    report = acquire_archives(
        owner, client, fixed, group="edinet-metrics", asof=DAY, lookback_days=0
    )
    assert report["status"] == "failed"
    assert report["list_failed_days"] == [DAY.isoformat()]
    assert report["list_unavailable_days"] == []
    assert report["downloaded"] == 1
    assert owner.freeze().archive_keys == frozenset({("S100ONE", 5)})


def test_pending_work_blocks_new_http(tmp_path):
    owner, client, _, lists = setup(tmp_path, [filing("S100ONE")])
    (owner.directory / "pending.json").write_text("{}")
    with pytest.raises(LakePublishError, match="resume_work"):
        acquire_archives(owner, client, lists, group="edinet-metrics", asof=DAY, lookback_days=0)
    client.fetch_archive.assert_not_called()


def test_original_publication_failure_stops_next_get_and_resumes_same_receipt(tmp_path):
    owner, client, _, lists = setup(tmp_path, [filing("S100ONE"), filing("S100TWO", "67580")])
    put_file = owner.store.put_file
    owner.store.put_file = Mock(side_effect=LakePublishError("injected storage failure"))
    with pytest.raises(LakePublishError, match="storage failure"):
        acquire_archives(owner, client, lists, group="edinet-metrics", asof=DAY, lookback_days=0)
    client.fetch_archive.assert_called_once_with("S100TWO", 5)
    assert (owner.directory / "pending.json").exists()
    owner.store.put_file = put_file
    fixed = owner.resume_work()
    assert fixed.archive_keys == frozenset({("S100TWO", 5)})
    assert fixed.metadata("S100TWO", 5).received_at_utc == AT
    client.fetch_archive.assert_called_once()


def test_fixed_native_xbrl_reader_supports_fresh_fact_extraction(tmp_path):
    owner, client, _, lists = setup(tmp_path, [filing("S100YR5P", ticker="18730")])
    acquire_archives(owner, client, lists, group="edinet-facts", asof=DAY, initialize_facts=True)
    fixed = owner.freeze()
    outputs = []
    for name in ("first", "cold"):
        database = tmp_path / f"{name}.sqlite"
        materialize_lists(lists, database)
        with closing(open_connection(database)) as connection, connection:
            merge_scope(connection, fixed.scope)
            assert (
                connection.execute("SELECT count(*) FROM edinet_source_archives").fetchone()[0] == 0
            )
            assert (
                connection.execute("SELECT count(*) FROM edinet_segment_facts").fetchone()[0] == 0
            )
        assert (
            extract_edinet_facts_command(
                asof=DAY, sqlite_path=database, reader=fixed, stdout=io.StringIO()
            )
            == 0
        )
        with sqlite3.connect(database) as connection:
            rows = connection.execute(
                "SELECT * FROM edinet_segment_facts ORDER BY 1,2,3,4"
            ).fetchall()
            assert rows
            outputs.append(rows)
    assert outputs[0] == outputs[1]
    assert owner.freeze().identity == fixed.identity


def test_xbrl_identity_does_not_satisfy_csv_original(tmp_path):
    owner, client, _, lists = setup(tmp_path, [filing("S100ONE")])
    xbrl = EdinetResponse(AT - timedelta(days=1), source_zip())
    owner.publish([NativeArchive.from_response("S100ONE", 1, xbrl)])
    csv = _edinet_csv_zip()
    client.fetch_archive.return_value = EdinetResponse(AT, csv)
    report = acquire_archives(
        owner, client, lists, group="edinet-metrics", asof=DAY, lookback_days=0
    )
    client.fetch_archive.assert_called_once_with("S100ONE", 5)
    assert report["downloaded"] == 1
    fixed = owner.freeze()
    assert fixed.archive_keys == frozenset({("S100ONE", 1), ("S100ONE", 5)})
    assert fixed.read_archive("S100ONE", 1) == xbrl.body
    assert fixed.metadata("S100ONE", 1).received_at_utc == xbrl.received_at
    assert fixed.read_archive("S100ONE", 5) == csv


def test_unrelated_native_row_mismatch_cannot_block_uninitialized_holder_get(tmp_path, monkeypatch):
    owner, client, _, lists = setup(
        tmp_path,
        [
            filing(" S100UNRELATED ", docTypeCode="160"),
            filing("S100HOLDER", docTypeCode="350", secCode=None),
        ],
    )
    monkeypatch.setattr("baibai_jobs.acquisition.edinet_archive_native.time.sleep", Mock())
    report = acquire_archives(owner, client, lists, group="edinet-facts", asof=DAY)
    assert report["status"] == "received"
    assert report["annual_archive_plan"] == "skipped_not_initialized"
    client.fetch_archive.assert_called_once_with("S100HOLDER", 1)


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("group", ["edinet-metrics", "edinet-facts"])
def test_archive_refusal_stops_after_partial_original_and_resumes_missing_only(
    tmp_path, status, group
):
    owner, client, _, lists = setup(
        tmp_path, [filing("S100ONE"), filing("S100TWO", "67580"), filing("S100THREE", "94320")]
    )
    client.fetch_archive.side_effect = [
        EdinetResponse(AT, source_zip()),
        EdinetAccessDeniedError(status),
    ]
    options = {"lookback_days": 0} if group == "edinet-metrics" else {"initialize_facts": True}
    report = acquire_archives(owner, client, lists, group=group, asof=DAY, **options)
    assert client.fetch_archive.call_count == 2
    assert report["status"] == "failed"
    assert report["downloaded"] == 1
    assert len(report["failed"]) == 1
    first = owner.freeze()
    assert len(first.archive_keys) == 1
    doc_id, source_type = client.fetch_archive.call_args_list[0].args
    retained = first.metadata(doc_id, source_type)
    client.reset_mock()
    client.fetch_archive.side_effect = None
    client.fetch_archive.return_value = EdinetResponse(AT + timedelta(hours=1), source_zip())
    resumed = acquire_archives(owner, client, lists, group=group, asof=DAY, **options)
    assert resumed["status"] == "received"
    assert resumed["downloaded"] == 2
    assert client.fetch_archive.call_count == 2
    assert owner.freeze().metadata(doc_id, source_type) == retained


@pytest.mark.parametrize(
    "failure",
    [
        EdinetAcquisitionError("EDINET request failed with status 404"),
        EdinetResponse(AT, b"invalid ZIP"),
    ],
)
def test_document_specific_failure_does_not_stop_other_archive_requests(tmp_path, failure):
    owner, client, _, lists = setup(tmp_path, [filing("S100ONE"), filing("S100TWO", "67580")])
    client.fetch_archive.side_effect = [failure, EdinetResponse(AT, source_zip())]
    report = acquire_archives(
        owner, client, lists, group="edinet-metrics", asof=DAY, lookback_days=0
    )
    assert client.fetch_archive.call_count == 2
    assert report["status"] == "failed"
    assert report["downloaded"] == 1


@pytest.mark.parametrize("group", ["edinet-metrics", "edinet-facts"])
def test_official_body_401_stops_after_saved_original_and_resumes_missing_only(tmp_path, group):
    from tests.engine.data_collection.providers.edinet.test_edinet_collection_http import Session
    from tests.helpers.http_doubles import response

    from baibai_engine.data_collection.providers.edinet.http import EDINETHTTP

    owner, _, _, lists = setup(
        tmp_path, [filing("S100ONE"), filing("S100TWO", "67580"), filing("S100THREE", "94320")]
    )
    replies = [response(source_zip()), response(b'{"StatusCode":401}'), response(source_zip())]
    session = Session(*replies)
    options = {"lookback_days": 0} if group == "edinet-metrics" else {"initialize_facts": True}
    report = acquire_archives(
        owner, EDINETHTTP("synthetic", session=session), lists, group=group, asof=DAY, **options
    )
    assert len(session.calls) == 2
    assert [item.close_count for item in replies] == [1, 1, 0]
    assert report["status"] == "failed"
    assert report["downloaded"] == 1
    assert report["failed"][0]["reason"] == "EdinetAccessDeniedError"
    fixed = owner.freeze()
    key = next(iter(fixed.archive_keys))
    original = fixed.metadata(*key)
    resumed_session = Session(response(source_zip()), response(source_zip()))
    resumed = acquire_archives(
        owner,
        EDINETHTTP("synthetic", session=resumed_session),
        lists,
        group=group,
        asof=DAY,
        **options,
    )
    assert resumed["status"] == "received"
    assert len(resumed_session.calls) == resumed["downloaded"] == 2
    assert resumed["retained_archives_reused"] == 1
    assert owner.freeze().metadata(*key) == original


@pytest.mark.parametrize("group", ["edinet-metrics", "edinet-facts"])
def test_http_429_with_broken_body_stops_after_original_and_resumes_missing_only(
    tmp_path, monkeypatch, group
):
    import requests
    from tests.engine.data_collection.providers.edinet.test_edinet_collection_http import Session
    from tests.helpers.http_doubles import response

    from baibai_engine.data_collection.providers.edinet.http import EDINETHTTP

    monkeypatch.setattr("time.sleep", lambda _: None)
    owner, _, _, lists = setup(
        tmp_path, [filing("S100ONE"), filing("S100TWO", "67580"), filing("S100THREE", "94320")]
    )
    refusals = []
    for _ in range(6):
        reply = requests.Response()
        reply.status_code = 429
        reply.headers["Retry-After"] = "0"
        reply.raw = Mock()
        reply.raw.stream.side_effect = requests.ConnectionError("broken refusal body")
        refusals.append(reply)
    later = response(source_zip())
    session = Session(response(source_zip()), *refusals, later)
    options = {"lookback_days": 0} if group == "edinet-metrics" else {"initialize_facts": True}
    report = acquire_archives(
        owner, EDINETHTTP("synthetic", session=session), lists, group=group, asof=DAY, **options
    )
    assert len(session.calls) == 7
    assert later.close_count == 0
    assert report["status"] == "failed"
    assert report["downloaded"] == 1
    assert report["failed"][0]["reason"] == "EdinetRateLimitError"
    assert all(reply.raw.stream.call_count == 0 for reply in refusals)
    assert all(
        reply.raw.close.call_count == reply.raw.release_conn.call_count == 1 for reply in refusals
    )
    fixed = owner.freeze()
    key = next(iter(fixed.archive_keys))
    original = fixed.metadata(*key)
    resumed_session = Session(response(source_zip()), response(source_zip()))
    resumed = acquire_archives(
        owner,
        EDINETHTTP("synthetic", session=resumed_session),
        lists,
        group=group,
        asof=DAY,
        **options,
    )
    assert resumed["status"] == "received"
    assert len(resumed_session.calls) == resumed["downloaded"] == 2
    assert resumed["retained_archives_reused"] == 1
    assert owner.freeze().metadata(*key) == original
