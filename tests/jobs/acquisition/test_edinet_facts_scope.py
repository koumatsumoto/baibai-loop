"""保存したfacts取得scopeはL2結果と独立に保持する。"""

import io
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock

import pytest
from tests.helpers.edinet_metrics import _edinet_document
from tests.helpers.edinet_research_sources import source_zip
from tests.jobs.acquisition.test_edinet_archive_native_acquisition import setup

from baibai_engine.data_collection.providers.edinet.archive_native import FixedEdinetArchives
from baibai_engine.data_collection.providers.edinet.archives import (
    read_source_archive,
    retain_source_archive,
)
from baibai_engine.data_collection.providers.edinet.facts_scope import (
    FactsScope,
    merge_scope,
    read_scope,
)
from baibai_engine.data_collection.providers.edinet.models import (
    EdinetRateLimitError,
    EdinetResponse,
)
from baibai_engine.data_collection.providers.edinet.receipts import retain_list_response
from baibai_engine.machine_analysis.observations.disclosures.edinet_list_rebuild import (
    rebuild_edinet_document_day,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.service import (
    extract_edinet_facts_command,
)
from baibai_engine.machine_analysis.observations.financials.edinet_reader import EDINETReader
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.edinet_fact_plan import prepare_fact_acquisition
from baibai_jobs.integration.edinet_list_native import materialize_lists
from baibai_jobs.integration.source_cache_merge import merge_stores
from baibai_jobs.maintenance.edinet_extraction import refresh_edinet_facts_command

DAY = date(2026, 11, 5)
AT = datetime(2026, 11, 5, 12, tzinfo=UTC)


def filing(doc_id, year, ticker="72030", **extra):
    return {
        **_edinet_document(ticker=ticker, doc_id=doc_id),
        "docInfoEditStatus": "0",
        "periodStart": f"{year - 1}-04-01",
        "periodEnd": f"{year}-03-31",
        "submitDateTime": f"{year}-06-01 12:00",
        **extra,
    }


def save_list(path, documents):
    body = json.dumps(
        {
            "metadata": {"resultset": {"count": len(documents)}},
            "results": [{"seqNumber": i, **doc} for i, doc in enumerate(documents, 1)],
        }
    ).encode()
    with closing(open_connection(path)) as connection:
        count = connection.execute("SELECT count(*) FROM edinet_list_responses").fetchone()[0]
        retain_list_response(
            connection, doc_date=DAY, received_at=AT + timedelta(seconds=count), body=body
        )
    rebuild_edinet_document_day(path, DAY)


def scope(path):
    with closing(sqlite3.connect(path)) as connection:
        return read_scope(connection)


@pytest.mark.parametrize("raw_only", [False, True])
def test_saved_scope_and_originals_do_not_depend_on_l2_status(tmp_path, raw_only):
    new = tmp_path / "market.sqlite"
    save_list(
        new,
        [
            filing("S100OLD", 2023),
            filing("S100UNRELATED", 2024),
            filing("S100SINCE", 2025),
            filing("S100LATEST", 2026),
        ],
    )
    with closing(open_connection(new)) as connection, connection:
        if raw_only:
            retain_source_archive(connection, "S100UNRELATED", 1, source_zip(), stored_on=DAY)
        merge_scope(
            connection,
            FactsScope(
                date(2025, 1, 1),
                frozenset({"S100OLD", "S100LOST"} | ({"S100UNRELATED"} if raw_only else set())),
                frozenset({"a" * 64}),
            ),
        )
    assert scope(new).since == date(2025, 1, 1)
    assert scope(new).doc_ids == frozenset(
        {"S100OLD", "S100LOST"} | ({"S100UNRELATED"} if raw_only else set())
    )
    assert len(scope(new).legacy_origins) == 1
    with sqlite3.connect(new) as connection:
        connection.execute("DELETE FROM source_coverage WHERE source='edinet_research_facts'")
        connection.execute(
            "INSERT INTO source_coverage VALUES ('edinet_research_facts','initialized',NULL,NULL,'bad',0,'ok','not-json')"
        )
    selected = prepare_fact_acquisition(new, asof=DAY)
    assert {doc["docID"] for doc in selected} == (
        {"S100OLD", "S100SINCE", "S100LATEST"} | ({"S100UNRELATED"} if raw_only else set())
    )
    # 原本の保持と取得scopeは抽出statusから独立する。
    with closing(open_connection(new)) as connection:
        for document in selected:
            doc_id = document["docID"]
            if read_source_archive(connection, doc_id, 1) is None:
                retain_source_archive(connection, doc_id, 1, source_zip(), stored_on=DAY)
    before_scope = scope(new)
    with sqlite3.connect(new) as connection:
        originals = connection.execute(
            "SELECT * FROM edinet_source_archives ORDER BY doc_id,source_type"
        ).fetchall()
        connection.execute("DELETE FROM edinet_segment_facts")
        connection.execute("DELETE FROM edinet_debt_schedule")
        connection.execute("DELETE FROM source_coverage WHERE source='edinet_research_facts'")
    assert (
        extract_edinet_facts_command(
            asof=DAY, sqlite_path=new, reader=EDINETReader(new), stdout=io.StringIO()
        )
        == 0
    )
    assert scope(new) == before_scope
    with closing(sqlite3.connect(new)) as connection:
        assert (
            connection.execute(
                "SELECT * FROM edinet_source_archives ORDER BY doc_id,source_type"
            ).fetchall()
            == originals
        )
    assert "S100LOST" in scope(new).doc_ids
    assert ("S100UNRELATED" in scope(new).doc_ids) is raw_only


def test_requested_scope_without_global_since_does_not_enable_fetches(tmp_path):
    path = tmp_path / "market.sqlite"
    save_list(path, [filing("S100RAW", 2026)])
    saved = FactsScope(doc_ids=frozenset({"S100RAW"}), legacy_origins=frozenset({"a" * 64}))
    with closing(open_connection(path)) as connection, connection:
        retain_source_archive(connection, "S100RAW", 1, source_zip(), stored_on=DAY)
        merge_scope(connection, saved)
    assert scope(path) == saved
    assert prepare_fact_acquisition(path, asof=DAY) is None


def test_selected_unavailable_and_failed_ids_survive_family_change_and_merge(tmp_path):
    local, canonical = tmp_path / "local.sqlite", tmp_path / "canonical.sqlite"
    old = filing("S100OLD", 2025, xbrlFlag="0")
    save_list(local, [old])
    with closing(open_connection(local)) as connection, connection:
        merge_scope(connection, FactsScope(since=DAY))
    assert [doc["docID"] for doc in prepare_fact_acquisition(local, asof=DAY)] == ["S100OLD"]
    # Later normal family appears while the old requested family becomes usable.
    save_list(local, [{**old, "xbrlFlag": "1"}, filing("S100LATEST", 2026)])
    assert {doc["docID"] for doc in prepare_fact_acquisition(local, asof=DAY)} == {
        "S100OLD",
        "S100LATEST",
    }
    open_connection(canonical).close()
    merge_stores(local, canonical)
    assert scope(canonical) == scope(local)
    older = tmp_path / "older.sqlite"
    open_connection(older).close()
    with closing(open_connection(older)) as connection, connection:
        merge_scope(connection, FactsScope(doc_ids=frozenset({"S100FAILED"})))
    merge_stores(older, canonical)
    assert scope(canonical).since == DAY
    assert scope(canonical).doc_ids == frozenset({"S100OLD", "S100LATEST", "S100FAILED"})


@pytest.mark.parametrize("ticker_only", [False, True])
def test_raw_success_not_l2_success_enables_scope_and_ticker_stays_local(
    tmp_path, monkeypatch, ticker_only
):
    path = tmp_path / "market.sqlite"
    owner, acquisition, _, _ = setup(tmp_path, [filing("S100CURRENT", 2026)])
    body = acquisition.fetch_archive.return_value.body
    owner.store.close = Mock()
    monkeypatch.setattr(
        "baibai_jobs.maintenance.edinet_extraction.Boto3R2Store", lambda **_: owner.store
    )
    # Archive is valid ZIP but later L2 read fails. This cannot undo source scope.
    monkeypatch.setattr(
        FixedEdinetArchives, "read_xbrl_zip", Mock(side_effect=ValueError("synthetic L2 failure"))
    )
    assert (
        refresh_edinet_facts_command(
            asof=DAY,
            sqlite_path=path,
            acquisition=acquisition,
            initialize=True,
            tickers=("7203",) if ticker_only else (),
            stdout=io.StringIO(),
        )
        == 1
    )
    assert scope(path).since == (None if ticker_only else DAY)
    assert scope(path).doc_ids == frozenset({"S100CURRENT"})
    assert owner.freeze().scope == scope(path)
    assert owner.freeze().read_archive("S100CURRENT", 1) == body
    assert EDINETReader(path).read_xbrl_zip("S100CURRENT") == body


def test_initial_partial_raw_failure_keeps_ids_and_zip_without_enabling(tmp_path, monkeypatch):
    body = source_zip()
    path = tmp_path / "market.sqlite"
    owner, acquisition, _, _ = setup(
        tmp_path, [filing("S100FIRST", 2026), filing("S100NEXT", 2026, ticker="83060")]
    )
    owner.store.close = Mock()
    monkeypatch.setattr(
        "baibai_jobs.maintenance.edinet_extraction.Boto3R2Store", lambda **_: owner.store
    )
    acquisition.fetch_archive.side_effect = [
        EdinetResponse(received_at=AT, body=body),
        EdinetRateLimitError("synthetic 429"),
    ]
    assert (
        refresh_edinet_facts_command(
            asof=DAY,
            sqlite_path=path,
            acquisition=acquisition,
            initialize=True,
            stdout=io.StringIO(),
        )
        == 1
    )
    assert scope(path).since is None
    assert scope(path).doc_ids == frozenset({"S100FIRST", "S100NEXT"})
    assert owner.freeze().scope == scope(path)
    assert owner.freeze().read_xbrl_zip("S100FIRST") == body
    assert EDINETReader(path).read_xbrl_zip("S100FIRST") == body


def test_offline_initial_extraction_does_not_enable_future_gets(tmp_path):
    path = tmp_path / "market.sqlite"
    save_list(path, [filing("S100CURRENT", 2026)])
    with closing(open_connection(path)) as connection:
        retain_source_archive(connection, "S100CURRENT", 1, source_zip(), stored_on=DAY)
    assert (
        extract_edinet_facts_command(
            asof=DAY,
            sqlite_path=path,
            reader=EDINETReader(path),
            initialize=True,
            stdout=io.StringIO(),
        )
        == 0
    )
    assert scope(path) == FactsScope()


def test_stale_scope_union_never_narrows_date_or_loses_provenance(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as connection, connection:
        merge_scope(
            connection, FactsScope(date(2025, 1, 1), frozenset({"OLD"}), frozenset({"a" * 64}))
        )
        merge_scope(
            connection, FactsScope(date(2026, 1, 1), frozenset({"NEW"}), frozenset({"b" * 64}))
        )
        merge_scope(connection, FactsScope(doc_ids=frozenset({"FAILED"})))
    assert scope(path) == FactsScope(
        date(2025, 1, 1), frozenset({"OLD", "NEW", "FAILED"}), frozenset({"a" * 64, "b" * 64})
    )


def test_missing_raw_get_failure_does_not_relabel_reusable_l2_as_extraction_failure(
    tmp_path, monkeypatch
):
    from baibai_engine.data_collection.providers.edinet.models import EdinetAcquisitionError
    from baibai_engine.machine_analysis.observations.financials.edinet_facts.store import (
        extraction_status,
    )

    path = tmp_path / "market.sqlite"
    owner, acquisition, _, lists = setup(tmp_path, [filing("S100CURRENT", 2026)])
    materialize_lists(lists, path)
    reader = Mock()
    reader.read_xbrl_zip.return_value = source_zip()
    assert (
        extract_edinet_facts_command(
            asof=DAY, sqlite_path=path, reader=reader, initialize=True, stdout=io.StringIO()
        )
        == 0
    )
    with closing(open_connection(path)) as connection, connection:
        merge_scope(connection, FactsScope(since=DAY, doc_ids=frozenset({"S100CURRENT"})))
    before = extraction_status(path)
    owner.publish((), scope=scope(path))
    owner.store.close = Mock()
    monkeypatch.setattr(
        "baibai_jobs.maintenance.edinet_extraction.Boto3R2Store", lambda **_: owner.store
    )
    acquisition.fetch_archive.side_effect = EdinetAcquisitionError("synthetic unavailable")
    assert (
        refresh_edinet_facts_command(
            asof=DAY, sqlite_path=path, acquisition=acquisition, stdout=io.StringIO()
        )
        == 1
    )
    assert extraction_status(path) == before
    acquisition.fetch_archive.assert_called_once_with("S100CURRENT", 1)
    assert reader.read_xbrl_zip.call_count == 1


def test_scope_union_locks_before_read_and_leaves_commit_to_caller(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as initial, initial:
        merge_scope(initial, FactsScope(doc_ids=frozenset({"OLD"})))
    with (
        closing(sqlite3.connect(path)) as first,
        closing(sqlite3.connect(path, timeout=0)) as second,
    ):
        statements = []
        first.set_trace_callback(statements.append)
        merge_scope(first, FactsScope(since=DAY))
        assert statements[0] == "BEGIN IMMEDIATE"
        assert first.in_transaction
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            merge_scope(second, FactsScope(doc_ids=frozenset({"NEW"})))
        first.commit()
        with second:
            merge_scope(second, FactsScope(doc_ids=frozenset({"NEW"})))
    assert scope(path) == FactsScope(since=DAY, doc_ids=frozenset({"OLD", "NEW"}))
