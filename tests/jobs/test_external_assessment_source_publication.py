"""Canonical HTML originals must lead the typed publication and fixed readback."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing

import pytest
from tests.engine.test_external_assessment_sources import monex_html
from tests.engine.test_minkabu import (
    no_coverage_html,
)
from tests.engine.test_minkabu import html as minkabu_html
from tests.jobs.external_assessment_publication_fixtures import (
    RECEIVED,
    integrate_ready_plans,
    pair_responses,
    ready_reference,
    upload_bundle,
)
from tests.jobs.external_assessment_publication_fixtures import _plan as _plan
from tests.jobs.external_assessment_publication_fixtures import _stage as _stage
from tests.jobs.external_assessment_publication_fixtures import publication as publication
from tests.jobs.test_external_assessment_raw import (
    Session,
)
from tests.jobs.test_external_assessment_raw import connect as connect

import baibai_jobs.acquisition.external_assessments.daily as daily
import baibai_jobs.integration.external_assessments as integration
import baibai_jobs.publication.snapshot_publication as publisher
from baibai_engine.data_collection.receipts.public_pages.receipts import read_responses
from baibai_engine.machine_analysis.observations.external_assessments.contract import (
    ANALYST_RATING_TABLE,
    MODEL_VALUATION_TABLE,
    QUANTS_SCORE_TABLE,
)
from baibai_engine.machine_analysis.observations.external_assessments.normalization import (
    ParseError,
)
from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import (
    decode_attempt as external_assessment_decode_attempt,
)
from baibai_engine.machine_analysis.observations.external_assessments.rebuild import (
    rebuild_attempts,
)
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.hydrate import hydrate_market_store
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.sqlite.source_cache.equity_web_import import (
    import_responses as equity_web_import_responses,
)
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    write_lake_store_origin,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.external_assessments.cohorts import new_result, record_outcome
from baibai_jobs.acquisition.external_assessments.collection import (
    CollectionOutcome as ExternalAssessmentCollectionOutcome,
)
from baibai_jobs.acquisition.external_assessments.staging import CohortStage, stage_bytes, stage_key
from baibai_jobs.integration.external_assessment_ready import available_ready
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.external_assessment_verification import (
    history_digests as external_assessment_history_digests,
)
from baibai_jobs.publication.public_page_verification import (
    history_digests as equity_web_history_digests,
)


def _publish(publication, plan, suffix):
    return integrate_ready_plans(publication, [plan], suffix)[0]


def _source_count(database):
    with closing(sqlite3.connect(database)) as conn:
        return conn.execute("SELECT COUNT(*) FROM equity_web_source_responses").fetchone()[0]


def test_three_independent_sources_and_rakuten_retry_rebuild_from_l1_only(publication):
    store, previous, _, database, _, directory = publication
    monex = pair_responses("monex", "7203", received_at_utc=RECEIVED, value="--")
    retry = pair_responses("rakuten", "7203", received_at_utc=RECEIVED, malformed=True)[:1]
    rakuten = pair_responses("rakuten", "7203", received_at_utc=RECEIVED, value="3.25")
    kabumap = pair_responses("kabumap", "7203", received_at_utc=RECEIVED)
    entries = [
        (
            source,
            "7203",
            "observed",
            responses[0].attempt_id,
            external_assessment_decode_attempt(responses),
            200,
        )
        for source, responses in (("monex", monex), ("rakuten", rakuten), ("kabumap", kabumap))
    ]
    plans = []
    for entry, originals in zip(entries, (monex, (*retry, *rakuten), kabumap), strict=True):
        source = entry[0]
        plan = _plan(previous, run_id=f"{source}-three-media", sources=(source,))
        _stage(store, plan, 0, [entry], originals)
        plans.append(plan)
    results = integrate_ready_plans(publication, plans, "three-media")
    assert len(results) == 3
    assert all(result["readback"] == "verified" for result in results)
    assert sum(result["rows_added"] for result in results) == 3
    assert sum(result["source_responses_added"] for result in results) == 5
    assert external_assessment_decode_attempt(monex).consensus_target is None
    assert _source_count(database) == 5
    assert len(equity_web_history_digests(database, months={(2026, 9)})) == 1

    pointer = publisher._serving_pointer(store).pointer
    source_mirror = directory / "source-only-mirror"
    release = publisher._resolve_serving_release(store, source_mirror, pointer)
    for partition in release.dataset_manifests[integration.SOURCE_RESPONSES_DATASET].partitions:
        for obj in partition.objects:
            publisher._fetch(store, source_mirror, obj.key, expected_sha256=obj.sha256)
    clean = directory / "source-only.sqlite"
    open_connection(clean).close()
    # Partial hydration repairs an already-bound release. This new fixture is
    # empty, so bind it without copying any published L2 rows into it.
    with closing(sqlite3.connect(clean)) as conn:
        write_lake_store_origin(conn, LakeStoreOrigin(release.release_id, release.manifest_sha256))
        conn.commit()
    with lake_session() as session:
        hydrate_market_store(
            session,
            release=release,
            cache=LakeObjectCache(root=source_mirror, source=LocalMirrorSource(source_mirror)),
            store=clean,
            dataset_names=(integration.SOURCE_RESPONSES_DATASET,),
            catalog=LAKE_DATASETS,
        )
    tables = (MODEL_VALUATION_TABLE, ANALYST_RATING_TABLE, QUANTS_SCORE_TABLE)
    with closing(sqlite3.connect(clean)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM equity_web_source_responses").fetchone() == (5,)
        assert [
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables
        ] == [0, 0, 0]
        assert read_responses(conn, (retry[0].receipt_id,)) == retry

    selected = (monex[0].attempt_id, rakuten[0].attempt_id, kabumap[0].attempt_id)
    assert rebuild_attempts(clean, attempt_ids=selected) == 3
    with (
        closing(sqlite3.connect(database)) as published,
        closing(sqlite3.connect(clean)) as rebuilt,
    ):
        for table, observation in zip(tables, (item[4] for item in entries), strict=True):
            key = ("7203", observation.fetched_at_utc.isoformat(timespec="microseconds"))
            query = f"SELECT * FROM {table} WHERE ticker=? AND fetched_at_utc=?"
            expected = published.execute(query, key).fetchone()
            assert expected is not None
            assert rebuilt.execute(query, key).fetchone() == expected
            assert rebuilt.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (1,)
    with pytest.raises(ParseError, match="incomplete_attempt"):
        rebuild_attempts(clean, attempt_ids=(retry[0].attempt_id,))
    with closing(sqlite3.connect(clean)) as conn:
        assert [
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables
        ] == [1, 1, 1]


@pytest.mark.parametrize(
    ("source", "missing"), [("monex", False), ("minkabu", False), ("minkabu", True)]
)
def test_actual_receiver_bundle_flows_into_fixed_release(publication, connect, source, missing):
    store, previous, _, database, _, _ = publication
    plan = _plan(previous, run_id="source-receiver-integration", sources=(source,))
    html = monex_html("7203") if source == "monex" else minkabu_html("7203")
    replies = (
        [(404, b"not found", "utf-8"), (200, no_coverage_html("7203").encode(), "utf-8")]
        if missing
        else [(200, html.encode(), "utf-8")]
    )
    connect(Session(replies))
    daily.acquire_cohort(store, plan, 0)
    report = _publish(publication, plan, "receiver-integration")
    assert report["readback"] == "verified"
    assert report["source_responses_added"] == report["rows_added"] == 1
    assert _source_count(database) == 1


def test_old_original_history_and_existing_derived_rows_roundtrip_in_clean_hydrate(publication):
    store, plan, _, database, _, directory = publication
    older = pair_responses("monex", "9999", received_at_utc=RECEIVED)
    assert equity_web_import_responses(database, older) == 1
    report = _publish(publication, plan, "clean-replay")
    assert report["readback"] == "verified"
    assert report["source_responses_added"] == 2
    assert _source_count(database) == 3
    pointer = publisher._serving_pointer(store).pointer
    mirror = directory / "clean-replay-mirror"
    release = publisher._resolve_serving_release(store, mirror, pointer)
    for manifest in release.dataset_manifests.values():
        for partition in manifest.partitions:
            for obj in partition.objects:
                publisher._fetch(store, mirror, obj.key, expected_sha256=obj.sha256)
    restored = directory / "clean-replay.sqlite"
    open_connection(restored).close()
    with lake_session() as session:
        hydrate_market_store(
            session,
            release=release,
            cache=LakeObjectCache(root=mirror, source=LocalMirrorSource(mirror)),
            store=restored,
            dataset_names=release.dataset_names(),
            catalog=LAKE_DATASETS,
        )
    assert equity_web_history_digests(restored, months={(2026, 9)}) == equity_web_history_digests(
        database, months={(2026, 9)}
    )
    assert external_assessment_history_digests(restored) == external_assessment_history_digests(
        database
    )


def test_malformed_http_200_publishes_original_without_typed_row(publication):
    store, previous, _, database, _, _ = publication
    plan = _plan(previous, run_id="source-raw-only")
    malformed = pair_responses("monex", "7203", received_at_utc=RECEIVED, malformed=True)
    _stage(
        store,
        plan,
        0,
        [("monex", "7203", "parse_failure", malformed[0].attempt_id, None, 200)],
        malformed,
    )
    before = external_assessment_history_digests(database)
    report = _publish(publication, plan, "raw-only")
    assert report["readback"] == "verified"
    assert report["source_responses_added"] == 1
    assert report["rows_added"] == report["observations"] == 0
    assert external_assessment_history_digests(database) == before


def test_raw_only_retry_publishes_previously_imported_original(publication, monkeypatch):
    store, previous, _, database, _, _ = publication
    plan = _plan(previous, run_id="source-raw-only-retry")
    malformed = pair_responses("monex", "7203", received_at_utc=RECEIVED, malformed=True)
    _stage(
        store,
        plan,
        0,
        [("monex", "7203", "parse_failure", malformed[0].attempt_id, None, 200)],
        malformed,
    )
    pointer = publisher._serving_pointer(store)
    typed_before = external_assessment_history_digests(database)
    original_digests = integration.equity_web_history_digests

    def fail_after_local_import(*args, **kwargs):
        original_digests(*args, **kwargs)
        raise RuntimeError("synthetic validation interruption")

    with monkeypatch.context() as patch:
        patch.setattr(integration, "equity_web_history_digests", fail_after_local_import)
        with pytest.raises(RuntimeError, match="synthetic validation interruption"):
            _publish(publication, plan, "raw-only-interrupted")
    assert _source_count(database) == 1
    assert external_assessment_history_digests(database) == typed_before
    assert publisher._serving_pointer(store) == pointer

    report = _publish(publication, plan, "raw-only-resumed")
    assert report["readback"] == "verified"
    assert report["source_responses_added"] == report["rows_added"] == 0
    assert publisher._serving_pointer(store) != pointer
    references, failed = available_ready(store)
    assert references == failed == []


def test_nonresponse_failure_has_no_original_or_typed_row(publication):
    store, previous, _, database, _, _ = publication
    plan = _plan(previous, run_id="source-no-response")
    _stage(store, plan, 0, [("monex", "7203", "http_failure", str(uuid.uuid4()), None, 503)])
    report = _publish(publication, plan, "no-response")
    assert report["readback"] == "verified"
    assert report["source_responses_added"] == report["rows_added"] == 0
    assert _source_count(database) == 0


def test_later_missing_bundle_keeps_earlier_l1_but_no_l2_or_pointer(publication):
    store, previous, _, database, _, _ = publication
    plan = _plan(previous, run_id="source-later-missing", tickers=("6758", "7203"), cohort_size=1)
    first = pair_responses("monex", "6758", received_at_utc=RECEIVED)
    _stage(
        store,
        plan,
        0,
        [
            (
                "monex",
                "6758",
                "observed",
                first[0].attempt_id,
                external_assessment_decode_attempt(first),
                200,
            )
        ],
        first,
    )
    second = pair_responses("monex", "7203", received_at_utc=RECEIVED)
    journal = record_outcome(
        plan,
        new_result(plan, 1),
        ExternalAssessmentCollectionOutcome(
            "monex", "7203", "observed", None, 200, second[0].attempt_id
        ),
        external_assessment_decode_attempt(second),
    )
    second_key = upload_bundle(store, plan, 1, second)
    withheld = store.values.pop(second_key)
    store.store_object(
        stage_key(plan, 1),
        stage_bytes(plan, CohortStage(journal, raw_bundle_keys=(second_key,))),
    )
    pointer = publisher._serving_pointer(store)
    typed_before = external_assessment_history_digests(database)
    reference = ready_reference(store, plan)
    state = integration.merge_ready(store, [reference], database=database)
    assert len(state["failed"]) == 1
    assert state["plans"] == []
    assert _source_count(database) == 1
    assert external_assessment_history_digests(database) == typed_before
    assert publisher._serving_pointer(store) == pointer
    store.values[second_key] = withheld
    report = _publish(publication, plan, "missing-retry")
    assert report["readback"] == "verified"
    assert report["source_responses_added"] == 1
    assert report["rows_added"] == 2
    assert _source_count(database) == 2


@pytest.mark.parametrize(
    "fault", ["wrong_run", "wrong_cohort", "wrong_pair", "wrong_attempt", "staged_mismatch"]
)
def test_mismatched_original_or_stage_blocks_l2_and_pointer(publication, fault):
    store, previous, _, database, _, _ = publication
    plan = _plan(previous, run_id=f"source-{fault}")
    selected = pair_responses("monex", "7203", received_at_utc=RECEIVED)
    original = selected
    bundle_plan = plan
    bundle_index = 0
    if fault == "wrong_run":
        bundle_plan = _plan(previous, run_id="different-run")
    elif fault == "wrong_cohort":
        bundle_index = 1
    elif fault == "wrong_pair":
        original = pair_responses("monex", "6758", received_at_utc=RECEIVED)
    elif fault == "wrong_attempt":
        original = pair_responses("monex", "7203", received_at_utc=RECEIVED)
    observation = external_assessment_decode_attempt(selected)
    if fault == "staged_mismatch":
        observation = observation.model_copy(update={"consensus_target": 999999.0})
    journal = record_outcome(
        plan,
        new_result(plan, 0),
        ExternalAssessmentCollectionOutcome(
            "monex", "7203", "observed", None, 200, selected[0].attempt_id
        ),
        observation,
    )
    key = upload_bundle(store, bundle_plan, bundle_index, original)
    store.store_object(
        stage_key(plan, 0), stage_bytes(plan, CohortStage(journal, raw_bundle_keys=(key,)))
    )
    pointer = publisher._serving_pointer(store)
    typed_before = external_assessment_history_digests(database)
    reference = ready_reference(store, plan)
    state = integration.merge_ready(store, [reference], database=database)
    assert len(state["failed"]) == 1
    assert state["plans"] == []
    assert external_assessment_history_digests(database) == typed_before
    assert publisher._serving_pointer(store) == pointer


@pytest.mark.parametrize("owner", ["original", "observation"])
@pytest.mark.parametrize("mutation", ["layer", "layout"])
def test_readback_checks_owner_contract_before_materializing(owner, mutation):
    """Fixed resolution is not production admission, but readback still checks its owner."""
    from types import SimpleNamespace

    from baibai_engine.data_collection.receipts.public_pages.snapshot_contracts import (
        EQUITY_WEB_SOURCE_RESPONSES,
    )
    from baibai_engine.machine_analysis.observations.external_assessments.snapshot_contracts import (
        EXTERNAL_ASSESSMENT_ANALYST_FORECAST_OBSERVATIONS,
    )
    from baibai_engine.storage.snapshots.reader import LakeReadError
    from baibai_jobs.publication import external_assessment_verification, public_page_verification

    descriptor = (
        EQUITY_WEB_SOURCE_RESPONSES
        if owner == "original"
        else EXTERNAL_ASSESSMENT_ANALYST_FORECAST_OBSERVATIONS
    )
    manifest = SimpleNamespace(
        dataset=descriptor.name,
        layer=("l2_processed" if descriptor.layer == "l1_source" else "l1_source")
        if mutation == "layer"
        else descriptor.layer,
        contract_version=descriptor.contract_version,
        partition_by=("year",) if mutation == "layout" else descriptor.partition_by,
        partitions=(),
    )
    release = SimpleNamespace(
        dataset_manifests={descriptor.name: manifest},
        dataset_manifest=lambda _name: manifest,
    )
    with pytest.raises(LakeReadError, match="layer|partition layout"):
        if owner == "original":
            public_page_verification.verify_release_history(
                None, release=release, cache=None, expected_history={}, selected_digests={}
            )
        else:
            external_assessment_verification.verify_release_history(
                None, release=release, cache=None, expected_history={}, observations=()
            )
