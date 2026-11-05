"""Finite facts scope migration preserves requested originals, not L2 claims."""

import io
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from tests.helpers.edinet_metrics import _edinet_document
from tests.helpers.edinet_research_sources import source_zip

from baibai_engine.data_collection.providers.edinet.archives import retain_source_archive
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
from baibai_engine.storage.sqlite.source_cache.facts_scope_upgrade import (
    upgrade_facts_scope_working_copy,
)
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError, open_connection
from baibai_jobs.acquisition.edinet_archives import acquire_archives
from baibai_jobs.acquisition.edinet_fact_plan import prepare_fact_acquisition
from baibai_jobs.acquisition.inputs import export_source_input
from baibai_jobs.integration.edinet_handoff import export_handoff, import_handoff
from baibai_jobs.integration.source_cache_merge import merge_stores
from baibai_jobs.maintenance.edinet_extraction import refresh_edinet_facts_command
from baibai_jobs.maintenance.source_refresh.edinet import EDINETRefresh

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


def legacy(path, *, initialized='{"since":"2025-01-01"}', ids=("S100OLD", "S100LOST")):
    save_list(
        path,
        [
            filing("S100OLD", 2023),
            filing("S100UNRELATED", 2024),
            filing("S100SINCE", 2025),
            filing("S100LATEST", 2026),
        ],
    )
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE edinet_facts_scope")
        connection.execute("PRAGMA user_version=49")
        for key in ids:
            connection.execute(
                "INSERT INTO source_coverage VALUES ('edinet_research_facts',?,NULL,NULL,'legacy-clock',0,'failed','unavailable')",
                (key,),
            )
        if initialized is not None:
            connection.execute(
                "INSERT INTO source_coverage VALUES ('edinet_research_facts','initialized',NULL,NULL,'legacy-clock',0,'ok',?)",
                (initialized,),
            )


@pytest.mark.parametrize("raw_only", [False, True])
def test_migration_keeps_scope_and_all_originals_then_l2_cannot_change_gets(tmp_path, raw_only):
    old, new = tmp_path / "old.sqlite", tmp_path / "new.sqlite"
    legacy(old)
    if raw_only:
        with sqlite3.connect(old) as connection:
            retain_source_archive(connection, "S100UNRELATED", 1, source_zip(), stored_on=DAY)
    before = old.read_bytes()
    with pytest.raises(SQLiteSchemaError):
        open_connection(old)
    upgrade_facts_scope_working_copy(old, new)
    assert old.read_bytes() == before
    assert scope(new).since == date(2025, 1, 1)
    assert scope(new).doc_ids == frozenset(
        {"S100OLD", "S100LOST"} | ({"S100UNRELATED"} if raw_only else set())
    )
    assert len(scope(new).legacy_origins) == 1
    with sqlite3.connect(old) as original, sqlite3.connect(new) as migrated:
        for (table,) in original.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            assert (
                original.execute(f'SELECT * FROM "{table}"').fetchall()
                == migrated.execute(f'SELECT * FROM "{table}"').fetchall()
            )
    with sqlite3.connect(new) as connection:
        connection.execute("DELETE FROM source_coverage WHERE source='edinet_research_facts'")
        connection.execute(
            "INSERT INTO source_coverage VALUES ('edinet_research_facts','initialized',NULL,NULL,'bad',0,'ok','not-json')"
        )
    with patch("baibai_jobs.maintenance.source_refresh.edinet.EDINETHTTP") as http:
        http.return_value.fetch_archive.return_value = EdinetResponse(
            received_at=AT, body=source_zip()
        )
        report = acquire_archives(
            new, group="edinet-facts", asof=DAY, api_key="synthetic", retained=set()
        )
        assert report["status"] == "received"
        assert [call.args for call in http.return_value.fetch_archive.call_args_list] == [
            ("S100OLD", 1),
            ("S100SINCE", 1),
            ("S100LATEST", 1),
        ]
    before_scope = scope(new)
    with sqlite3.connect(new) as connection:
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
    assert "S100LOST" in scope(new).doc_ids
    assert ("S100UNRELATED" in scope(new).doc_ids) is raw_only
    with pytest.raises(FileExistsError):
        upgrade_facts_scope_working_copy(old, new)


@pytest.mark.parametrize(
    "value",
    [
        "{}",
        "null",
        '{"since":null}',
        '{"since":42}',
        '{"since":"2026-99-40"}',
        '{"since":"20261105"}',
        "bad-json",
    ],
)
def test_invalid_legacy_since_is_not_inferred(tmp_path, value):
    old, new = tmp_path / "old.sqlite", tmp_path / "new.sqlite"
    legacy(old, initialized=value)
    before = old.read_bytes()
    with pytest.raises((ValueError, TypeError)):
        upgrade_facts_scope_working_copy(old, new)
    assert old.read_bytes() == before
    assert not new.exists()


def test_uninitialized_and_stage_scope_never_enable_global(tmp_path):
    old, new = tmp_path / "old.sqlite", tmp_path / "new.sqlite"
    legacy(old, initialized=None)
    upgrade_facts_scope_working_copy(old, new)
    assert scope(new).since is None
    assert scope(new).doc_ids == frozenset({"S100OLD", "S100LOST"})
    assert prepare_fact_acquisition(new, asof=DAY) is None
    # Old raw-only handoff has no L2 status, but its requested type1 family survives.
    stage, out = tmp_path / "stage.sqlite", tmp_path / "stage50.sqlite"
    legacy(stage, ids=())
    with sqlite3.connect(stage) as connection:
        retain_source_archive(connection, "S100RAW", 1, source_zip(), stored_on=DAY)
    upgrade_facts_scope_working_copy(stage, out, stage=True)
    assert scope(out).since is None
    assert scope(out).doc_ids == frozenset({"S100RAW"})


def test_selected_unavailable_and_failed_ids_survive_family_change_and_handoff(tmp_path):
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
    handoff = tmp_path / "handoff.sqlite"
    export_handoff(local, handoff, since=AT, archives=set())
    import_handoff(handoff, canonical)
    assert scope(canonical) == scope(local)
    older = tmp_path / "older.sqlite"
    open_connection(older).close()
    with closing(open_connection(older)) as connection, connection:
        merge_scope(connection, FactsScope(doc_ids=frozenset({"S100FAILED"})))
    merge_stores(older, canonical)
    assert scope(canonical).since == DAY
    assert scope(canonical).doc_ids == frozenset({"S100OLD", "S100LATEST", "S100FAILED"})
    scoped = tmp_path / "input.sqlite"
    export_source_input(canonical, scoped, group="edinet-facts", asof=DAY)
    assert scope(scoped) == scope(canonical)
    with sqlite3.connect(scoped) as connection:
        assert (
            connection.execute(
                "SELECT * FROM source_coverage WHERE source='edinet_research_facts'"
            ).fetchall()
            == []
        )


@pytest.mark.parametrize("ticker_only", [False, True])
def test_raw_success_not_l2_success_enables_scope_and_ticker_stays_local(tmp_path, ticker_only):
    body = source_zip()
    path = tmp_path / "market.sqlite"
    save_list(path, [filing("S100CURRENT", 2026)])
    # Archive is valid ZIP but later L2 read fails. This cannot undo source scope.
    with patch("baibai_jobs.maintenance.source_refresh.edinet.EDINETHTTP") as http:
        http.return_value.fetch_archive.return_value = EdinetResponse(received_at=AT, body=body)
        acquisition = EDINETRefresh("synthetic", sqlite_path=path)
        reader = Mock()
        reader.read_xbrl_zip.side_effect = ValueError("synthetic L2 failure")
        assert (
            refresh_edinet_facts_command(
                asof=DAY,
                sqlite_path=path,
                reader=reader,
                acquisition=acquisition,
                initialize=True,
                tickers=("7203",) if ticker_only else (),
                stdout=io.StringIO(),
            )
            == 1
        )
        acquisition.close()
    assert scope(path).since == (None if ticker_only else DAY)
    assert scope(path).doc_ids == frozenset({"S100CURRENT"})
    assert EDINETReader(path).read_xbrl_zip("S100CURRENT") == body


def test_initial_partial_raw_failure_keeps_ids_and_zip_without_enabling(tmp_path):
    body = source_zip()
    path = tmp_path / "market.sqlite"
    save_list(path, [filing("S100FIRST", 2026), filing("S100NEXT", 2026, ticker="83060")])
    with patch("baibai_jobs.maintenance.source_refresh.edinet.EDINETHTTP") as http:
        http.return_value.fetch_archive.side_effect = [
            EdinetResponse(received_at=AT, body=body),
            EdinetRateLimitError("synthetic 429"),
        ]
        acquisition = EDINETRefresh("synthetic", sqlite_path=path)
        assert (
            refresh_edinet_facts_command(
                asof=DAY,
                sqlite_path=path,
                reader=EDINETReader(path),
                acquisition=acquisition,
                initialize=True,
                stdout=io.StringIO(),
            )
            == 1
        )
        acquisition.close()
    assert scope(path).since is None
    assert scope(path).doc_ids == frozenset({"S100FIRST", "S100NEXT"})
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


def test_missing_raw_get_failure_does_not_relabel_reusable_l2_as_extraction_failure(tmp_path):
    from baibai_engine.data_collection.providers.edinet.models import EdinetAcquisitionError
    from baibai_engine.machine_analysis.observations.financials.edinet_facts.store import (
        extraction_status,
    )

    path = tmp_path / "market.sqlite"
    save_list(path, [filing("S100CURRENT", 2026)])
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
    acquisition = Mock()
    acquisition.download_xbrl_zip.side_effect = EdinetAcquisitionError("synthetic unavailable")
    assert (
        refresh_edinet_facts_command(
            asof=DAY, sqlite_path=path, reader=reader, acquisition=acquisition, stdout=io.StringIO()
        )
        == 1
    )
    assert extraction_status(path) == before
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


@pytest.mark.parametrize("invalid_id", ["", b"S100BLOB"])
def test_invalid_legacy_id_is_rejected_before_copy_without_dropping_it(tmp_path, invalid_id):
    old, output = tmp_path / "old.sqlite", tmp_path / "new.sqlite"
    legacy(old, ids=(invalid_id,))
    before = old.read_bytes()
    with pytest.raises(ValueError, match="invalid legacy EDINET acquisition identity"):
        upgrade_facts_scope_working_copy(old, output)
    assert old.read_bytes() == before
    assert not output.exists()
