"""Facts native reader changes bytes access, not scope or extraction semantics."""

import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import pytest
from tests.helpers.edinet_metrics import _edinet_csv_zip
from tests.helpers.edinet_research_sources import source_zip
from tests.jobs.integration.test_edinet_metric_rebuild import AT, BASE, document, sources

from baibai_engine.data_collection.providers.edinet.archive_native import HEAD_KEY as ARCHIVE_HEAD
from baibai_engine.data_collection.providers.edinet.archive_native import (
    FixedEdinetArchives,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.archives import retain_source_archive
from baibai_engine.data_collection.providers.edinet.facts_scope import (
    FactsScope,
    read_scope,
)
from baibai_engine.data_collection.providers.edinet.list_native import HEAD_KEY as LIST_HEAD
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.machine_analysis.generation.edinet_inputs import (
    read_edinet_input,
    retain_edinet_input,
)
from baibai_engine.machine_analysis.observations.financials import edinet_integration as owner
from baibai_engine.machine_analysis.observations.financials.edinet_facts.store import (
    extraction_status,
    record_failure,
)
from baibai_engine.machine_analysis.observations.financials.edinet_reader import EDINETReader
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration import sources as integration
from baibai_jobs.integration.edinet_archive_native import (
    materialize_archives,
    project_archive_scope,
)
from baibai_jobs.integration.edinet_list_native import materialize_lists
from baibai_jobs.integration.edinet_metric_rebuild import rebuild_fixed_edinet_metrics


@pytest.fixture
def work(tmp_path):
    with TemporaryDirectory(prefix="case-", dir=tmp_path) as directory:
        yield Path(directory)


def setup(work, *, mode="complete", days=None):
    docs = [document("S100FACT1", "18730"), document("S100FACT2", "20000")]
    bodies = {("S100HOLDER", 1): b"unrelated holder original"}
    if mode != "all_missing":
        bodies[("S100FACT1", 1)] = b"invalid ZIP" if mode == "invalid_zip" else source_zip()
    if mode in {"complete", "invalid_zip", "unavailable"}:
        bodies[("S100FACT2", 1)] = source_zip()
    if mode == "unavailable":
        docs[0]["xbrlFlag"] = "0"
    store, list_owner, archive_owner, lists, _ = sources(
        work, days=days or {BASE: docs}, bodies=bodies
    )
    archives = archive_owner.publish(
        [], scope=FactsScope(BASE, frozenset({"S100FACT1", "S100FACT2"}), frozenset({"a" * 64}))
    )
    saved = retain_edinet_input(store, lists, archives, selected_at=AT)
    fixed = read_edinet_input(store, saved)
    return store, list_owner, archive_owner, fixed


def project(work, fixed, name, *, sql_zip=False, initialized=True):
    path = work / name
    materialize_lists(fixed.lists, path)
    if sql_zip:
        materialize_archives(fixed.archives, path)
    elif initialized:
        with closing(open_connection(path)) as connection, connection:
            project_archive_scope(fixed.archives, connection)
    return path


def rows(path):
    with closing(sqlite3.connect(path)) as connection:
        return (
            connection.execute("SELECT * FROM edinet_segment_facts ORDER BY 1,2,3,4").fetchall(),
            connection.execute("SELECT * FROM edinet_debt_schedule ORDER BY 1,2,3,4").fetchall(),
            connection.execute(
                "SELECT coverage_key,coverage_start,coverage_end,record_count,status,error "
                "FROM source_coverage WHERE source='edinet_research_facts' ORDER BY coverage_key"
            ).fetchall(),
            read_scope(connection),
        )


def run(path, fixed=None, *, asof=BASE):
    return owner.extract_saved_edinet(
        path,
        asof=asof,
        lookback_days=0,
        **({"fact_archives": fixed.archives} if fixed is not None else {}),
    )


def forbid_sql(*args):
    pytest.fail("facts attempted an SQL ZIP read")


@pytest.mark.parametrize("supply_metrics", [False, True])
@pytest.mark.parametrize("supply_facts", [False, True])
def test_fixed_readers_use_independent_inventory_and_only_missing_readers_query_sql(
    work, monkeypatch, supply_metrics, supply_facts
):
    _, _, archive_owner, fixed = setup(work, mode="partial")
    archives = archive_owner.publish(
        [NativeArchive.from_response("S100FACT1", 5, EdinetResponse(AT, _edinet_csv_zip()))]
    )
    path = project(work, fixed, "mixed.sqlite")
    # Deliberately disjoint inventories expose accidental fixed/SQL cross-routing.
    with closing(open_connection(path)) as connection, connection:
        retain_source_archive(connection, "S100FACT2", 1, source_zip(), stored_on=BASE)
        retain_source_archive(connection, "S100FACT2", 5, _edinet_csv_zip(), stored_on=BASE)
    sql_inventory = Mock(
        side_effect=AssertionError("both fixed readers must not query SQL archive inventory")
        if supply_metrics and supply_facts
        else None,
        wraps=owner.archive_keys,
    )
    metric_extraction = Mock(wraps=owner.extract_edinet_metrics_from_plan)
    monkeypatch.setattr(owner, "archive_keys", sql_inventory)
    monkeypatch.setattr(owner, "extract_edinet_metrics_from_plan", metric_extraction)
    report = owner.extract_saved_edinet(
        path,
        asof=BASE,
        lookback_days=0,
        metric_archives=archives if supply_metrics else None,
        fact_archives=archives if supply_facts else None,
    )
    if supply_metrics and supply_facts:
        sql_inventory.assert_not_called()
    else:
        sql_inventory.assert_called_once()
    metric_extraction.assert_called_once()
    assert metric_extraction.call_args.kwargs["unavailable_doc_ids"] == frozenset(
        {"S100FACT2" if supply_metrics else "S100FACT1"}
    )
    selected_reader = metric_extraction.call_args.kwargs["reader"]
    if supply_metrics:
        assert selected_reader is archives
    else:
        assert isinstance(selected_reader, EDINETReader)
    assert set(extraction_status(path)) == {"S100FACT1" if supply_facts else "S100FACT2"}
    assert report["edinet-facts"]["missing_archive_count"] == 1
    assert report["edinet-metrics"]["missing_archive_count"] == 1


@pytest.mark.parametrize(
    "mode", ["complete", "partial", "all_missing", "invalid_zip", "unavailable"]
)
def test_native_facts_match_legacy_results_and_rows_without_sql_zip(work, monkeypatch, mode):
    store, _, _, fixed = setup(work, mode=mode)
    legacy = project(work, fixed, "legacy.sqlite", sql_zip=True)
    expected = run(legacy)
    expected_rows = rows(legacy)
    native = project(work, fixed, "native.sqlite")
    del store.values[LIST_HEAD]
    del store.values[ARCHIVE_HEAD]
    store.get_keys.clear()
    before = dict(store.values)
    monkeypatch.setattr(EDINETReader, "read_xbrl_zip", forbid_sql)
    actual = run(native, fixed)
    assert actual == expected
    assert rows(native) == expected_rows
    assert store.values == before
    assert LIST_HEAD not in store.get_keys
    assert ARCHIVE_HEAD not in store.get_keys
    holder = fixed.archives.metadata("S100HOLDER", 1)
    assert not any(holder.sha256 in key for key in store.get_keys)
    with closing(sqlite3.connect(native)) as connection:
        assert connection.execute("SELECT count(*) FROM edinet_source_archives").fetchone() == (0,)
    if mode in {"partial", "all_missing"}:
        facts = actual["edinet-facts"]
        assert facts["status"] == "completed"  # Preserve the existing report contract.
        assert "xbrl_archives_unavailable" in facts["deferred_inputs"]
        missing = "S100FACT2"
        assert missing not in extraction_status(native)
    if mode == "invalid_zip":
        assert actual["edinet-facts"]["status"] == "failed"


@pytest.mark.parametrize("supply_facts", [False, True])
def test_reader_does_not_initialize_scope_or_make_metrics_enable_facts(work, supply_facts):
    store, _, _, fixed = setup(work)
    path = project(work, fixed, "native.sqlite", initialized=False)
    before = path.read_bytes()
    store.get_keys.clear()
    report = owner.extract_saved_edinet(
        path,
        asof=BASE,
        lookback_days=0,
        metric_archives=fixed.archives,
        **({"fact_archives": fixed.archives} if supply_facts else {}),
    )
    assert report["edinet-facts"]["status"] == "skipped_not_initialized"
    assert not any(key.endswith(".zip") for key in store.get_keys)
    assert path.read_bytes() == before
    assert not rows(path)[0]


def test_metrics_only_fixed_input_does_not_implicitly_route_initialized_facts(work):
    store, _, _, fixed = setup(work)
    path = project(work, fixed, "native.sqlite")
    store.get_keys.clear()
    report = owner.extract_saved_edinet(
        path, asof=BASE, lookback_days=0, metric_archives=fixed.archives
    )
    assert report["edinet-facts"]["missing_archive_count"] == 2
    assert not rows(path)[0]
    assert not any(key.endswith(".zip") for key in store.get_keys)


def test_successful_facts_reuse_existing_status_without_any_zip_get(work, monkeypatch):
    store, _, _, fixed = setup(work)
    path = project(work, fixed, "native.sqlite")
    first = run(path, fixed)
    assert first["edinet-facts"]["counts"]["extracted"] == 2
    before = path.read_bytes()
    store.get_keys.clear()
    monkeypatch.setattr(type(fixed.archives), "read_xbrl_zip", forbid_sql)
    report = run(path, fixed)
    assert report["edinet-facts"]["counts"]["reused"] == 2
    assert not any(key.endswith(".zip") for key in store.get_keys)
    assert path.read_bytes() == before


def test_prior_failed_extraction_is_retried_without_source_scope_change(work):
    _, _, _, fixed = setup(work)
    path = project(work, fixed, "native.sqlite")
    record_failure(
        path, doc_id="S100FACT1", disclosed_on=BASE.isoformat(), reason="old parse failure"
    )
    report = run(path, fixed)
    assert report["edinet-facts"]["counts"]["extracted"] == 2
    assert extraction_status(path)["S100FACT1"][0] == "ok"
    with closing(sqlite3.connect(path)) as connection:
        assert read_scope(connection) == fixed.archives.scope


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_referenced_body_damage_is_fatal_even_with_sql_copy(work, monkeypatch, damage):
    store, _, _, fixed = setup(work)
    path = project(work, fixed, "native.sqlite", sql_zip=True)
    metadata = fixed.archives.metadata("S100FACT1", 1)
    key = f"l1/edinet/archives/v1/objects/{metadata.sha256}.zip"
    if damage == "missing":
        del store.values[key]
    else:
        store.store_object(key, b"changed ZIP", sha256=metadata.sha256)
    before = path.read_bytes()
    monkeypatch.setattr(EDINETReader, "read_xbrl_zip", forbid_sql)
    with pytest.raises(LakePublishError):
        run(path, fixed)
    assert path.read_bytes() == before
    assert extraction_status(path) == {}


def test_sql_only_archive_cannot_fill_a_missing_fixed_native_member(work, monkeypatch):
    _, _, _, fixed = setup(work, mode="partial")
    path = project(work, fixed, "native.sqlite")
    with closing(open_connection(path)) as connection, connection:
        retain_source_archive(connection, "S100FACT2", 1, source_zip(), stored_on=BASE)
    monkeypatch.setattr(EDINETReader, "read_xbrl_zip", forbid_sql)
    report = run(path, fixed)["edinet-facts"]
    assert report["counts"]["extracted"] == 1
    assert report["missing_archive_count"] == 1
    assert "S100FACT2" not in extraction_status(path)


@pytest.mark.parametrize("event", ["correction", "withdrawal", "edit"])
def test_native_byte_route_keeps_retained_annual_operations(work, monkeypatch, event):
    origin = document("S100FACT1", "18730")
    changed = {**origin, "docID": "S100FACT2", "submitDateTime": "2026-04-27 12:00"}
    if event == "correction":
        changed.update(docTypeCode="130", parentDocID="S100FACT1")
    elif event == "withdrawal":
        changed.update(
            parentDocID="S100FACT1", withdrawalStatus="1", opeDateTime="2026-04-27 12:00"
        )
    else:
        changed.update(docID="S100FACT1", docInfoEditStatus="1", opeDateTime="2026-04-27 12:00")
    day = BASE + timedelta(days=3)
    _, _, _, fixed = setup(work, days={BASE: [origin], day: [changed]})
    legacy = project(work, fixed, "legacy.sqlite", sql_zip=True)
    expected = run(legacy, asof=day)
    expected_rows = rows(legacy)
    native = project(work, fixed, "native.sqlite")
    monkeypatch.setattr(EDINETReader, "read_xbrl_zip", forbid_sql)
    assert run(native, fixed, asof=day) == expected
    assert rows(native) == expected_rows


def test_normal_apply_passes_the_same_pair_to_metrics_and_facts_without_sql_zip_copy(
    work, monkeypatch
):
    store, _, _, fixed = setup(work)
    monkeypatch.setenv("BAIBAI_EDINET_LIST_NATIVE", "true")
    monkeypatch.setattr(integration, "Boto3R2Store", lambda **_: store)
    directory = work / "integration"
    assert integration.main(["plan", "--directory", str(directory)]) == 0
    path = work / "market.sqlite"
    with closing(open_connection(path)):
        pass
    spy = Mock(wraps=owner.extract_saved_edinet)
    monkeypatch.setattr(integration, "extract_saved_edinet", spy)
    monkeypatch.setattr(EDINETReader, "read_xbrl_zip", forbid_sql)
    report = integration.apply(store, directory, market=path, asof=BASE)
    assert spy.call_args.kwargs["metric_archives"] is spy.call_args.kwargs["fact_archives"]
    assert spy.call_args.kwargs["fact_archives"].identity == fixed.archives.identity
    assert report["extractions"]["edinet-facts"]["counts"]["extracted"] == 2
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT count(*) FROM edinet_source_archives").fetchone() == (0,)
    assert (
        json.loads((directory / "applied.json").read_bytes())["edinet_input"]
        == report["edinet_input"]
    )


def test_existing_metrics_cold_rebuild_still_has_no_facts_scope_or_rows(work):
    store, _, archive_owner, fixed = setup(work)
    archives = archive_owner.publish(
        [NativeArchive.from_response("S100FACT1", 5, EdinetResponse(AT, _edinet_csv_zip()))]
    )
    fixed = read_edinet_input(
        store, retain_edinet_input(store, fixed.lists, archives, selected_at=AT)
    )
    output = work / "candidate.sqlite"
    report = rebuild_fixed_edinet_metrics(
        fixed.lists, fixed.archives, output, asof=BASE, lookback_days=0
    )
    assert report["output_written"]
    assert report["edinet-metrics"]["status"] == "partial"
    with closing(sqlite3.connect(output)) as connection:
        for table in (
            "edinet_source_archives",
            "edinet_facts_scope",
            "edinet_segment_facts",
            "edinet_debt_schedule",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


@pytest.mark.parametrize("cached", ["matching", "failed", "stale_revision", "changed_identity"])
def test_cached_status_does_not_replace_saved_partial_inventory(work, monkeypatch, cached):
    store, _, archive_owner, partial = setup(work, mode="partial")
    path = project(work, partial, "warm.sqlite")
    complete = archive_owner.publish(
        [NativeArchive.from_response("S100FACT2", 1, EdinetResponse(AT, source_zip()))]
    )
    later = read_edinet_input(
        store, retain_edinet_input(store, partial.lists, complete, selected_at=AT)
    )
    assert run(path, later)["edinet-facts"]["counts"]["extracted"] == 2
    if cached == "failed":
        record_failure(
            path, doc_id="S100FACT2", disclosed_on=BASE.isoformat(), reason="prior failure"
        )
    elif cached in {"stale_revision", "changed_identity"}:
        value = json.loads(extraction_status(path)["S100FACT2"][1])
        if cached == "stale_revision":
            value["revision"] = "old-extractor-revision"
        else:
            value["identity"]["ticker"] = "9999"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "UPDATE source_coverage SET error=? "
                "WHERE source='edinet_research_facts' AND coverage_key='S100FACT2'",
                (json.dumps(value, sort_keys=True),),
            )
    # The current owner has the missing original, but this older pair does not.
    assert ("S100FACT2", 1) in complete.archive_keys
    del store.values[LIST_HEAD]
    del store.values[ARCHIVE_HEAD]
    fixed = read_edinet_input(store, partial.identity)
    assert fixed.identity == partial.identity
    assert ("S100FACT2", 1) not in fixed.archives.archive_keys
    before_status = extraction_status(path)
    before_bytes = path.read_bytes()
    before_objects = dict(store.values)
    store.get_keys.clear()
    store.put_keys.clear()
    monkeypatch.setattr(EDINETReader, "read_xbrl_zip", forbid_sql)
    monkeypatch.setattr(FixedEdinetArchives, "read_xbrl_zip", forbid_sql)
    report = run(path, fixed)["edinet-facts"]
    assert report["exit_code"] == 0
    assert report["status"] == "completed"  # Existing facts report, not metrics semantics.
    if cached == "matching":
        assert report["counts"]["reused"] == 2
        assert "missing_archive_count" not in report
        assert "xbrl_archives_unavailable" not in report["deferred_inputs"]
    else:
        assert report["counts"]["reused"] == 1
        assert report["missing_archive_count"] == 1
        assert "xbrl_archives_unavailable" in report["deferred_inputs"]
        assert not report["counts"].get("failed", 0)
    assert extraction_status(path) == before_status
    assert path.read_bytes() == before_bytes
    assert store.values == before_objects
    assert store.put_keys == []
    assert not any(key.endswith(".zip") for key in store.get_keys)
    assert LIST_HEAD not in store.get_keys
    assert ARCHIVE_HEAD not in store.get_keys


def test_native_scope_cannot_erase_sql_retained_annual_family(work, monkeypatch):
    old = {
        **document("S100FACT1", "18730"),
        "periodStart": "2023-04-01",
        "periodEnd": "2024-03-31",
        "submitDateTime": "2024-04-01 12:00",
    }
    latest = document("S100FACT2", "18730")
    store, _, _, original = setup(work, days={BASE: [old, latest]})
    path = project(work, original, "scope.sqlite")
    with closing(sqlite3.connect(path)) as connection, connection:
        sql_scope = read_scope(connection)
    assert "S100FACT1" in sql_scope.doc_ids
    # Bytes access is native; this slice deliberately retains SQL scope semantics.
    value = original.archives.value()
    value["acquisition_scope"] = {"since": None, "doc_ids": [], "legacy_origins": []}
    uninitialized = FixedEdinetArchives(store, value)
    fixed = read_edinet_input(
        store, retain_edinet_input(store, original.lists, uninitialized, selected_at=AT)
    )
    assert fixed.archives.scope == FactsScope()
    del store.values[LIST_HEAD]
    del store.values[ARCHIVE_HEAD]
    monkeypatch.setattr(EDINETReader, "read_xbrl_zip", forbid_sql)
    before = dict(store.values)
    report = run(path, fixed)["edinet-facts"]
    assert report["status"] == "completed"
    assert report["counts"]["selected"] == 2
    assert report["counts"]["extracted"] == 2
    assert set(extraction_status(path)) == {"S100FACT1", "S100FACT2"}
    with closing(sqlite3.connect(path)) as connection, connection:
        assert read_scope(connection) == sql_scope
        assert connection.execute("SELECT count(*) FROM edinet_source_archives").fetchone() == (0,)
    assert store.values == before
