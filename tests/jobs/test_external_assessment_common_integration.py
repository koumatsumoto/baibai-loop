import sqlite3
import uuid
from contextlib import closing

import pytest
from tests.jobs.external_assessment_publication_fixtures import (
    RECEIVED,
    _plan,
    _stage,
    pair_responses,
)
from tests.jobs.external_assessment_publication_fixtures import (
    independent_plans as independent_plans,
)
from tests.jobs.external_assessment_publication_fixtures import publication as publication

from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import decode_attempt as external_assessment_decode_attempt
from baibai_engine.data_collection.external_assessment.verification import history_digests as external_assessment_history_digests
from baibai_engine.machine_analysis.observations.external_assessments.store import save_observations as external_assessment_save_observations
from baibai_jobs.jobs.external_assessment_integration import (
    VALUATION_DATASET,
    merge_ready,
    verify_and_seal,
)
from baibai_jobs.storage import external_assessment_staging as staging
from baibai_jobs.storage import publish_market_lake as publisher
from baibai_jobs.storage.external_assessment_ready import load_ready, prepare_plan, save_ready
from baibai_jobs.storage.lake_publish import LakePublishError


def ready_references(batch):
    store, selected, *_ = batch
    return [
        save_ready(store, prepare_plan(store, staging.read_plan(store, entry["run_id"])))
        for entry in selected
    ]


def test_ready_freezes_exact_stages_without_publishing_or_relying_on_mutable_checkpoints(
    independent_plans,
):
    store, selected, _, database, _, _ = independent_plans
    pointer = publisher._serving_pointer(store)
    before = database.read_bytes()
    references = ready_references(independent_plans)
    plan = staging.read_plan(store, selected[0]["run_id"])
    store.store_object(staging.stage_key(plan, 0), b"mutable checkpoint replaced by a retry")
    fixed = load_ready(store, references[0])
    assert fixed.plan.plan_sha256 == plan.plan_sha256
    assert fixed.accounting["attempted_completeness"] is True
    assert publisher._serving_pointer(store) == pointer
    assert database.read_bytes() == before


def test_two_ready_sources_use_one_common_publication_and_keep_separate_verified_terminals(
    independent_plans,
):
    store, selected, handle, database, mirror, directory = independent_plans
    references = ready_references(independent_plans)
    before = publisher._serving_pointer(store)
    state = merge_ready(store, references, database=database)
    assert publisher._serving_pointer(store) == before
    assert state["failed"] == []
    # A separate source legitimately updates its facts in this same integration.
    with closing(sqlite3.connect(database)) as conn, conn:
        conn.execute("UPDATE jquants_daily_bars SET close = close + 1")
    published = publisher.publish_market_lake(sqlite_path=database, mirror_root=mirror, store=store)
    results = verify_and_seal(store, state, handle=handle, directory=directory / "common-readback")
    assert len(results) == 2
    terminals = [
        staging.read_terminal(store, staging.read_plan(store, item["run_id"])) for item in selected
    ]
    assert {item["release_id"] for item in terminals} == {published.release_id}
    assert len({item["plan_sha256"] for item in terminals}) == 2
    assert all(item["readback"] == "verified" for item in terminals)


def test_common_readback_failure_never_repairs_sqlite_or_seals_any_terminal(
    independent_plans, monkeypatch
):
    store, selected, handle, database, mirror, directory = independent_plans
    state = merge_ready(store, ready_references(independent_plans), database=database)
    publisher.publish_market_lake(sqlite_path=database, mirror_root=mirror, store=store)
    before = database.read_bytes()

    def reject(*args, **kwargs):
        raise ValueError("synthetic independent readback failure")

    monkeypatch.setattr(
        "baibai_jobs.jobs.external_assessment_integration.external_assessment_verify_release_history",
        reject,
    )
    for _ in range(2):
        with pytest.raises(ValueError, match="independent readback"):
            verify_and_seal(store, state, handle=handle, directory=directory / "failed-readback")
        assert database.read_bytes() == before
        assert all(
            staging.read_terminal(store, staging.read_plan(store, item["run_id"])) is None
            for item in selected
        )


def test_explicit_partial_confirmation_keeps_access_halt_and_exact_partial_terminal(
    independent_plans,
):
    store, selected, handle, database, mirror, directory = independent_plans
    previous = staging.read_plan(store, selected[0]["run_id"])
    plan = _plan(
        previous, run_id="monex-common-partial", sources=("monex",), tickers=("7203", "6758")
    )
    responses = pair_responses("monex", "7203", received_at_utc=RECEIVED)
    observation = external_assessment_decode_attempt(responses)
    _stage(
        store,
        plan,
        0,
        [
            ("monex", "7203", "observed", responses[0].attempt_id, observation, 200),
            ("monex", "6758", "access_denied", str(uuid.uuid4()), None, 403),
        ],
        responses,
    )
    staging.save_new_plan(store, plan)
    original = store.values[staging.stage_key(plan, 0)].body
    with pytest.raises(ValueError, match="explicit confirmed partial"):
        prepare_plan(store, plan)
    reference = save_ready(store, prepare_plan(store, plan, confirmed_partial=True))
    state = merge_ready(store, [reference], database=database)
    assert state["failed"] == []
    publisher.publish_market_lake(sqlite_path=database, mirror_root=mirror, store=store)
    verify_and_seal(store, state, handle=handle, directory=directory / "partial-common-readback")
    terminal = staging.read_terminal(store, plan)
    assert terminal["state_counts"]["failure"] == 1
    assert terminal["observations"] == 1
    assert terminal["readback"] == "verified"
    assert store.values[staging.stage_key(plan, 0)].body == original


def test_failed_decode_in_another_month_keeps_originals_and_does_not_block_healthy_seal(
    independent_plans,
):
    store, selected, handle, database, mirror, directory = independent_plans
    previous = staging.read_plan(store, selected[0]["run_id"])
    failed = _plan(previous, run_id="monex-failed-common", sources=("monex",))
    responses = pair_responses("monex", "7203", received_at_utc=RECEIVED.replace(month=6))
    observation = external_assessment_decode_attempt(responses).model_copy(
        update={"consensus_target": 999999.0}
    )
    _stage(
        store,
        failed,
        0,
        [("monex", "7203", "observed", responses[0].attempt_id, observation, 200)],
        responses,
    )
    staging.save_new_plan(store, failed)
    bad_ref = save_ready(store, prepare_plan(store, failed))
    good = ready_references(independent_plans)[1]
    june = {(VALUATION_DATASET, 2026, 6)}
    typed_before = external_assessment_history_digests(database, partitions=june)
    state = merge_ready(store, [bad_ref, good], database=database)
    assert len(state["failed"]) == 1
    assert len(state["plans"]) == 1
    assert any(month == 6 for _, month, _ in state["source_history"])
    assert external_assessment_history_digests(database, partitions=june) == typed_before
    publisher.publish_market_lake(sqlite_path=database, mirror_root=mirror, store=store)
    results = verify_and_seal(store, state, handle=handle, directory=directory / "mixed-readback")
    assert len(results) == 1
    assert results[0]["readback"] == "verified"
    assert staging.read_terminal(store, failed) is None
    assert staging.read_terminal(store, staging.read_plan(store, selected[1]["run_id"])) is not None


@pytest.mark.parametrize("unexpected_forecast", [False, True])
def test_minkabu_fresh_forecast_readback_with_a_separate_tradingview_update(
    publication, unexpected_forecast
):
    store, previous, handle, database, mirror, directory = publication
    plan = _plan(previous, run_id="minkabu-common-forecast", sources=("minkabu",))
    responses = pair_responses("minkabu", "7203", received_at_utc=RECEIVED)
    observation = external_assessment_decode_attempt(responses)
    _stage(
        store,
        plan,
        0,
        [
            ("minkabu", "7203", "observed", responses[0].attempt_id, observation, 200),
        ],
        responses,
    )
    staging.save_new_plan(store, plan)
    reference = save_ready(store, prepare_plan(store, plan))
    state = merge_ready(store, [reference], database=database)
    assert state["failed"] == []
    with closing(sqlite3.connect(database)) as conn, conn:
        conn.execute(
            "INSERT INTO tradingview_forecast_snapshots "
            "(snapshot_date,ticker,provider_symbol,fetched_at_utc,fetch_status) VALUES (?,?,?,?,?)",
            ("2026-09-30", "7203", "TSE:7203", RECEIVED.isoformat(), "ok"),
        )
    if unexpected_forecast:
        external_assessment_save_observations(
            database,
            [
                observation.model_copy(
                    update={
                        "fetched_at_utc": RECEIVED.replace(month=8),
                        "snapshot_date": observation.snapshot_date.replace(month=8),
                    }
                )
            ],
        )
    publisher.publish_market_lake(sqlite_path=database, mirror_root=mirror, store=store)
    fresh = directory / "minkabu-fresh-readback"
    assert not fresh.exists()
    if unexpected_forecast:
        with pytest.raises(LakePublishError, match="unrelated external"):
            verify_and_seal(store, state, handle=handle, directory=fresh)
        assert staging.read_terminal(store, plan) is None
        return
    results = verify_and_seal(store, state, handle=handle, directory=fresh)
    assert results[0]["readback"] == "verified"
    assert staging.read_terminal(store, plan)["observations"] == 1
    assert any("analyst_forecast_observations" in str(path) for path in fresh.rglob("*.parquet"))


def test_terminal_failure_resumes_only_pending_source_from_fresh_hydration(
    independent_plans, monkeypatch
):
    from baibai_engine.storage.lake.objects import LakeObjectCache
    from baibai_engine.storage.lake.objects import LocalMirrorSource
    from baibai_engine.storage.observations.snapshot import create_snapshot as create_market_snapshot
    from baibai_engine.storage.lake.equity_web_verification import history_digests as equity_web_history_digests
    from baibai_engine.storage.lake.duck import lake_session
    from baibai_engine.storage.lake.hydrate import dehydrate_market_store, hydrate_market_store
    from baibai_jobs.jobs import external_assessment_integration as integration
    from baibai_jobs.storage.external_assessment_ready import available_ready

    store, selected, handle, database, mirror, directory = independent_plans
    before = publisher._serving_pointer(store).pointer
    metadata = directory / "retained-metadata.sqlite"
    create_market_snapshot(database, metadata)
    base = publisher._resolve_serving_release(store, mirror, before)
    dehydrate_market_store(metadata, release=base)
    references = ready_references(independent_plans)
    state = merge_ready(store, references, database=database)
    publisher.publish_market_lake(sqlite_path=database, mirror_root=mirror, store=store)
    put = integration.put_private
    failed_key = staging.prefix(selected[1]["run_id"]) + "/published.json"

    def fail_second(store, key, *args, **kwargs):
        if key == failed_key:
            raise LakePublishError("synthetic terminal response unavailable")
        return put(store, key, *args, **kwargs)

    monkeypatch.setattr(integration, "put_private", fail_second)
    with pytest.raises(LakePublishError, match="terminal response"):
        verify_and_seal(store, state, handle=handle, directory=directory / "first-readback")
    first_key = staging.prefix(selected[0]["run_id"]) + "/published.json"
    first_terminal = store.values[first_key].body
    assert failed_key not in store.values
    history = external_assessment_history_digests(database)
    originals = equity_web_history_digests(database, months={(2026, 9)})

    # A replacement runner starts from retained metadata, not this attempt's mutable DB.
    fresh = directory / "fresh.sqlite"
    fresh.write_bytes(metadata.read_bytes())
    fresh_mirror = directory / "fresh-mirror"
    current = publisher._serving_pointer(store).pointer
    fixed = publisher._resolve_serving_release(store, fresh_mirror, current)
    for manifest in fixed.dataset_manifests.values():
        for partition in manifest.partitions:
            for obj in partition.objects:
                publisher._fetch(store, fresh_mirror, obj.key, expected_sha256=obj.sha256)
    with lake_session() as session:
        hydrate_market_store(
            session,
            release=fixed,
            cache=LakeObjectCache(root=fresh_mirror, source=LocalMirrorSource(fresh_mirror)),
            store=fresh,
            dataset_names=fixed.dataset_names(),
        )
    assert external_assessment_history_digests(fresh) == history
    assert equity_web_history_digests(fresh, months={(2026, 9)}) == originals
    pending, failed = available_ready(store)
    assert failed == []
    assert pending == references[1:]
    resumed = merge_ready(store, pending, database=fresh)
    assert resumed["failed"] == []
    assert resumed["plans"][0]["rows_added"] == 0
    assert resumed["plans"][0]["source_responses_added"] == 0
    publisher.publish_market_lake(sqlite_path=fresh, mirror_root=fresh_mirror, store=store)
    monkeypatch.setattr(integration, "put_private", put)
    result = verify_and_seal(
        store, resumed, handle=handle, directory=directory / "resumed-readback"
    )
    assert len(result) == 1
    assert result[0]["source"] == "rakuten"
    assert result[0]["readback"] == "verified"
    assert store.values[first_key].body == first_terminal
    assert external_assessment_history_digests(fresh) == history
    assert equity_web_history_digests(fresh, months={(2026, 9)}) == originals
    assert available_ready(store) == ([], [])


def test_terminal_acknowledgement_loss_does_not_reopen_a_verified_source(
    independent_plans, monkeypatch
):
    from baibai_jobs.jobs import external_assessment_integration as integration
    from baibai_jobs.storage.external_assessment_ready import available_ready

    store, selected, handle, database, mirror, directory = independent_plans
    reference = ready_references(independent_plans)[0]
    state = merge_ready(store, [reference], database=database)
    publisher.publish_market_lake(sqlite_path=database, mirror_root=mirror, store=store)
    put = integration.put_private

    def saved_then_lost(*args, **kwargs):
        put(*args, **kwargs)
        raise LakePublishError("synthetic acknowledgement lost")

    monkeypatch.setattr(integration, "put_private", saved_then_lost)
    with pytest.raises(LakePublishError, match="acknowledgement lost"):
        verify_and_seal(store, state, handle=handle, directory=directory / "lost-ack")
    plan = staging.read_plan(store, selected[0]["run_id"])
    assert staging.read_terminal(store, plan)["readback"] == "verified"
    pending, failed = available_ready(store)
    assert failed == []
    assert reference not in pending
