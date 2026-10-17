from __future__ import annotations

import shlex
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from tests.batch.external_assessment_publication_fixtures import pair_responses, upload_bundle
from tests.helpers.l1_release import market_store
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.master import insert_master_rows
from tests.helpers.r2_store import MemoryR2Store
from tools.quality.drift.check_workflow_trust import check_workflow

from baibai_batch.jobs import external_assessment_publication as external_assessment
from baibai_batch.jobs.external_assessment_cohorts import (
    make_plan,
    new_result,
    record_outcome,
)
from baibai_batch.storage import publish_market_lake as publisher
from baibai_batch.storage.external_assessment_staging import (
    CohortStage,
    put_private,
    read_stages,
    stage_bytes,
    stage_key,
)
from baibai_batch.storage.lake_publish import LakeCASConflict, LakePublishError
from baibai_batch.storage.publication_lease import LeaseError, acquire, release, verify_held
from baibai_engine.batch_api import (
    ExternalAssessmentAnalystRatingObservation,
    ExternalAssessmentCollectionOutcome,
    ExternalAssessmentModelValuationObservation,
    ExternalAssessmentQuantsScoreObservation,
    LakeObjectCache,
    LocalMirrorSource,
    external_assessment_collector_binding,
    external_assessment_decode_attempt,
    external_assessment_history_digests,
    external_assessment_save_observations,
    external_assessment_verify_observations,
    external_assessment_verify_release_history,
    lake_session,
)

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/cloud-external-assessment.yml"
RECEIVED = datetime(2026, 9, 30, 15, tzinfo=UTC)


def test_acquire_workflow_timeout_can_start_a_harmless_command():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    commands = [
        shlex.split(step["run"])
        for step in workflow["jobs"]["acquire"]["steps"]
        if step.get("run", "").startswith("timeout ")
    ]
    assert len(commands) == 1
    command = commands[0]
    # Exercise GNU timeout's actual duration parser without provider or R2 I/O.
    result = subprocess.run(
        [*command[: command.index("uv")], "true"],
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0


@pytest.fixture
def publication(tmp_path, monkeypatch):
    datasets = (
        "jquants.master_snapshots",
        "jquants.daily_bars",
        "jquants.short_sale_reports",
        external_assessment.SOURCE_RESPONSES_DATASET,
        external_assessment.VALUATION_DATASET,
        external_assessment.RATING_DATASET,
        external_assessment.QUANTS_DATASET,
    )
    narrow_release_policy(monkeypatch, datasets=datasets)
    monkeypatch.setattr(publisher, "lake_verified_git_commit", lambda: "a" * 40)
    database = market_store(tmp_path / "market.sqlite")
    with closing(sqlite3.connect(database)) as connection, connection:
        insert_master_rows(
            connection, [("2026-09-30", "7203", "合成会社", "PRIME", "輸送用機器", "011")]
        )
    common = {
        "ticker": "7203",
        "fetched_at_utc": RECEIVED - timedelta(days=1),
        "source_url": "https://example.test/synthetic",
        "parser_version": "synthetic-1",
    }
    external_assessment_save_observations(
        database,
        [
            ExternalAssessmentModelValuationObservation(
                **common, distributor="monex", consensus_target=91
            ),
            ExternalAssessmentAnalystRatingObservation(**common, distributor="rakuten", rating=2),
            ExternalAssessmentModelValuationObservation(
                ticker="6758",
                fetched_at_utc=datetime(2026, 7, 31, 15, tzinfo=UTC),
                source_url="https://example.test/synthetic",
                parser_version="synthetic-1",
                distributor="monex",
                consensus_target=0,
                consensus_target_origin="synthetic-origin",
                consensus_target_count=0,
            ),
            ExternalAssessmentAnalystRatingObservation(
                ticker="6758",
                fetched_at_utc=datetime(2026, 7, 31, 15, tzinfo=UTC),
                source_url="https://example.test/synthetic",
                parser_version="synthetic-1",
                distributor="rakuten",
                rating=None,
                rating_updated_label="synthetic-label",
            ),
        ],
    )
    store = MemoryR2Store()
    mirror = tmp_path / "mirror"
    publisher.publish_market_lake(
        sqlite_path=database,
        mirror_root=mirror,
        store=store,
        release_id="before-retail-daily",
    )
    handle, _ = acquire(store, "operator")
    plan = make_plan(
        sources=("monex", "rakuten"),
        run_id="synthetic-daily",
        tickers=("7203", "6758"),
        universe_binding="synthetic-fixed-master",
        collector_binding=external_assessment_collector_binding(),
    )
    journal = new_result(plan, 0)
    originals = []
    for source, ticker in plan.cohorts[0].pairs:
        responses = pair_responses(
            source,
            ticker,
            received_at_utc=RECEIVED,
            value=("123,456円" if source == "monex" and ticker == "7203" else "--")
            if source == "monex"
            else "3.25",
            malformed=(source == "rakuten" and ticker == "6758"),
        )
        originals.extend(responses)
        observation = (
            None
            if source == "rakuten" and ticker == "6758"
            else external_assessment_decode_attempt(responses)
        )
        outcome = ExternalAssessmentCollectionOutcome(
            source,
            ticker,
            "observed" if observation else "parse_failure",
            None if observation else "no_estimates_identity_unconfirmed",
            200,
            responses[0].attempt_id,
        )
        journal = record_outcome(plan, journal, outcome, observation)
    bundle_key = upload_bundle(store, plan, 0, tuple(originals))
    put_private(
        store,
        stage_key(plan, 0),
        stage_bytes(plan, CohortStage(journal, raw_bundle_keys=(bundle_key,))),
        previous_etag=None,
    )
    return store, plan, handle, database, mirror, tmp_path


def test_stored_graph_readback_preserves_null_receipts_history_and_unrelated_data(
    publication,
    capsys,
):
    store, plan, handle, database, mirror, tmp_path = publication
    before = publisher._serving_pointer(store).pointer
    original = store.get_bytes(stage_key(plan, 0))
    report = external_assessment.publish_and_verify(
        store,
        plan,
        handle=handle,
        sqlite_path=database,
        mirror=mirror,
        directory=tmp_path / "readback",
    )
    assert report["rows_added"] == 3
    assert report["observations"] == 3
    assert report["attempted_completeness"] is True
    assert report["success_completeness"] is False
    assert report["readback"] == "verified"
    assert set(report["publication_transfers"]) == {
        "uploaded_objects",
        "reused_objects",
        "uploaded_bytes",
        "downloaded_bytes",
        "head_requests",
        "get_requests",
    }
    assert report["publication_transfers"]["uploaded_bytes"] == report["uploaded_bytes"]
    assert report["publication_transfers"]["downloaded_bytes"] > 0
    assert report["publication_transfers_scope"] == (
        "immutable_graph_and_pointer_only; "
        "excludes lease/stage/base-resolve/hydrate/independent-readback"
    )
    assert (
        report["current_reachable_expected_parquet_bytes"]
        > report["external_assessment_readback_expected_parquet_bytes"]
        > 0
    )
    assert report["external_assessment_readback_parquet_objects"] == 3
    assert report["source_responses_added"] == 6
    assert report["release_id"] != before.release_id
    assert store.get_bytes(stage_key(plan, 0)) == original
    assert not (tmp_path / "readback/readback.sqlite").exists()
    pointer = publisher._serving_pointer(store).pointer
    after = publisher._resolve_serving_release(store, tmp_path / "readback/mirror", pointer)
    assert len(after.dataset_manifests[external_assessment.VALUATION_DATASET].partitions) == 3
    for name, manifest in after.dataset_manifests.items():
        if name in {external_assessment.VALUATION_DATASET, external_assessment.RATING_DATASET}:
            for partition in manifest.partitions:
                for obj in partition.objects:
                    publisher._fetch(
                        store, tmp_path / "readback/mirror", obj.key, expected_sha256=obj.sha256
                    )
    with lake_session() as session:
        external_assessment_verify_release_history(
            session,
            release=after,
            cache=LakeObjectCache(
                root=tmp_path / "readback/mirror",
                source=LocalMirrorSource(tmp_path / "readback/mirror"),
            ),
            expected_history=external_assessment_history_digests(database),
            observations=(),
        )
    assert "123456" not in capsys.readouterr().out
    count = len(store.put_keys)
    repeat = external_assessment.publish_and_verify(
        store,
        plan,
        handle=handle,
        sqlite_path=database,
        mirror=mirror,
        directory=tmp_path / "repeat-readback",
    )
    assert repeat["rows_added"] == repeat["uploaded_bytes"] == repeat["uploaded_objects"] == 0
    assert repeat["publication_transfers"] is None
    assert len(store.put_keys) == count
    external_assessment_verify_observations(
        database,
        [
            entry.observation
            for entry in read_stages(store, plan)[0][0].result.entries
            if entry.observation is not None
        ],
    )


def test_stale_origin_is_rejected_before_stage_changes_or_canonical_upload(publication):
    store, plan, handle, database, mirror, tmp_path = publication
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE lake_store_origin SET release_id='stale-generation'")
    before = external_assessment_history_digests(database)
    puts = len(store.put_keys)
    with pytest.raises(LakePublishError, match="fresh current hydrate"):
        external_assessment.publish_and_verify(
            store,
            plan,
            handle=handle,
            sqlite_path=database,
            mirror=mirror,
            directory=tmp_path / "rejected",
        )
    assert external_assessment_history_digests(database) == before
    assert len(store.put_keys) == puts


def test_readback_gets_only_external_assessment_parquet_and_keeps_new_object_byte_verification(
    publication, monkeypatch
):
    store, plan, handle, database, mirror, tmp_path = publication
    before_keys = set(store.values)
    forbidden = {key for key in before_keys if key.endswith(".parquet")}
    assert any("jquants.master_snapshots" in key for key in forbidden)
    assert any("jquants.daily_bars" in key for key in forbidden)
    download, get = store.download_file, store.get_bytes

    def download_only_external_assessment(key, path, *, expect_bytes=None):
        assert key not in forbidden, "unchanged Parquet GET is forbidden"
        return download(key, path, expect_bytes=expect_bytes)

    def get_only_external_assessment(key):
        assert key not in forbidden, "unchanged Parquet GET is forbidden"
        return get(key)

    monkeypatch.setattr(store, "download_file", download_only_external_assessment)
    monkeypatch.setattr(store, "get_bytes", get_only_external_assessment)
    store.get_keys.clear()
    external_assessment.publish_and_verify(
        store,
        plan,
        handle=handle,
        sqlite_path=database,
        mirror=mirror,
        directory=tmp_path / "retail-only-readback",
    )
    new_parquet = {key for key in store.values.keys() - before_keys if key.endswith(".parquet")}
    assert len(new_parquet) == 3
    # Typed history uses the publisher's verified remote bytes, not the export files.
    assert all(store.get_keys.count(key) == 1 for key in new_parquet)
    assert not forbidden.intersection(store.get_keys)
    assert not (tmp_path / "retail-only-readback/readback.sqlite").exists()


def test_corrupt_external_assessment_byte_readback_cannot_switch_pointer_or_mark_published(
    publication, monkeypatch
):
    store, plan, handle, database, mirror, tmp_path = publication
    directory = tmp_path / "corrupt-readback"
    before = publisher._serving_pointer(store)
    download = store.download_file

    def corrupt_readback(key, path, *, expect_bytes=None):
        download(key, path, expect_bytes=expect_bytes)
        if key.endswith(".parquet") and path.is_relative_to(directory):
            path.write_bytes(b"synthetic-corrupt-parquet")

    monkeypatch.setattr(store, "download_file", corrupt_readback)
    with pytest.raises(LakePublishError, match="bytes postcondition failed"):
        external_assessment.publish_and_verify(
            store,
            plan,
            handle=handle,
            sqlite_path=database,
            mirror=mirror,
            directory=directory,
        )
    assert not any(key.endswith("/published.json") for key in store.values)
    assert publisher._serving_pointer(store) == before


def test_fixed_release_refetches_retained_bytes_from_a_different_generation(
    publication, monkeypatch
):
    store, plan, handle, database, mirror, tmp_path = publication
    before_keys = set(store.values)
    previous_bytes = next(
        item.body
        for key, item in store.values.items()
        if external_assessment.VALUATION_DATASET in key and key.endswith(".parquet")
    )
    publish = external_assessment.publish_market_lake
    replaced_keys = []

    def replace_retained_generation(**kwargs):
        result = publish(**kwargs)
        key = next(
            key
            for key in store.values.keys() - before_keys
            if external_assessment.VALUATION_DATASET in key and key.endswith(".parquet")
        )
        # A filename alone is insufficient: old verified bytes have another SHA.
        (kwargs["verified_readback_root"] / key).write_bytes(previous_bytes)
        replaced_keys.append(key)
        return result

    monkeypatch.setattr(external_assessment, "publish_market_lake", replace_retained_generation)
    store.get_keys.clear()
    report = external_assessment.publish_and_verify(
        store,
        plan,
        handle=handle,
        sqlite_path=database,
        mirror=mirror,
        directory=tmp_path / "generation-readback",
    )
    assert report["readback"] == "verified"
    key = replaced_keys[0]
    assert store.get_keys.count(key) == 2
    assert (tmp_path / "generation-readback/mirror" / key).read_bytes() == store.values[key].body


def fixed_external_assessment_readback(publication):
    store, _, _, database, _, tmp_path = publication
    mirror = tmp_path / "typed-readback"
    pointer = publisher._serving_pointer(store).pointer
    fixed = publisher._resolve_serving_release(store, mirror, pointer)
    for name in (external_assessment.VALUATION_DATASET, external_assessment.RATING_DATASET):
        for partition in fixed.dataset_manifests[name].partitions:
            for obj in partition.objects:
                publisher._fetch(store, mirror, obj.key, expected_sha256=obj.sha256)
    return fixed, LakeObjectCache(root=mirror, source=LocalMirrorSource(mirror)), database


def test_missing_old_month_history_fails_even_without_current_receipts(publication):
    fixed, cache, database = fixed_external_assessment_readback(publication)
    manifests = dict(fixed.dataset_manifests)
    original = manifests[external_assessment.VALUATION_DATASET]
    assert len(original.partitions) == 2
    manifests[external_assessment.VALUATION_DATASET] = original.model_copy(
        update={"partitions": original.partitions[1:]}
    )
    with lake_session() as session, pytest.raises(ValueError, match="changed received history"):
        external_assessment_verify_release_history(
            session,
            release=replace(fixed, dataset_manifests=manifests),
            cache=cache,
            expected_history=external_assessment_history_digests(database),
            observations=(),
        )


@pytest.mark.parametrize("updates", [{"rating": 1}, {"rating_updated_label": None}])
def test_typed_null_and_metadata_receipts_must_match_all_columns(publication, updates):
    fixed, cache, database = fixed_external_assessment_readback(publication)
    original = ExternalAssessmentAnalystRatingObservation(
        ticker="6758",
        fetched_at_utc=datetime(2026, 7, 31, 15, tzinfo=UTC),
        source_url="https://example.test/synthetic",
        parser_version="synthetic-1",
        distributor="rakuten",
        rating=None,
        rating_updated_label="synthetic-label",
    )
    with lake_session() as session:
        external_assessment_verify_release_history(
            session,
            release=fixed,
            cache=cache,
            expected_history=external_assessment_history_digests(database),
            observations=(original,),
        )
        with pytest.raises(ValueError, match="receipt readback mismatch"):
            external_assessment_verify_release_history(
                session,
                release=fixed,
                cache=cache,
                expected_history=external_assessment_history_digests(database),
                observations=(original.model_copy(update=updates),),
            )


def test_missing_external_assessment_dataset_is_empty_only_when_expected_history_is_empty(
    publication,
):
    fixed, cache, database = fixed_external_assessment_readback(publication)
    manifests = dict(fixed.dataset_manifests)
    del manifests[external_assessment.RATING_DATASET]
    with lake_session() as session, pytest.raises(ValueError, match="changed received history"):
        external_assessment_verify_release_history(
            session,
            release=replace(fixed, dataset_manifests=manifests),
            cache=cache,
            expected_history=external_assessment_history_digests(database),
            observations=(),
        )


def test_pointer_conflict_keeps_canonical_generation_and_does_not_mark_published(publication):
    store, plan, handle, database, mirror, tmp_path = publication
    before = publisher._serving_pointer(store).pointer
    store.conflict_pointer = True
    with pytest.raises(LakeCASConflict):
        external_assessment.publish_and_verify(
            store,
            plan,
            handle=handle,
            sqlite_path=database,
            mirror=mirror,
            directory=tmp_path / "conflicted",
        )
    assert publisher._serving_pointer(store).pointer == before
    assert not any(key.endswith("/published.json") for key in store.values)


def test_expired_replaced_or_released_lease_is_rejected_before_publication(publication):
    store, plan, handle, database, mirror, tmp_path = publication
    with pytest.raises(LeaseError):
        verify_held(store, handle, now=handle.expires_at_utc)
    with pytest.raises(LeaseError):
        verify_held(store, replace(handle, owner="local:" + "b" * 32))
    release(store, handle)
    puts = len(store.put_keys)
    with pytest.raises(LeaseError):
        external_assessment.publish_and_verify(
            store,
            plan,
            handle=handle,
            sqlite_path=database,
            mirror=mirror,
            directory=tmp_path / "lease-rejected",
        )
    assert len(store.put_keys) == puts


def test_missing_or_unattempted_cohort_cannot_publish(publication):
    store, plan, handle, database, mirror, tmp_path = publication
    key = stage_key(plan, 0)
    saved = store.values.pop(key)
    puts = len(store.put_keys)
    with pytest.raises(ValueError, match="missing cohort"):
        external_assessment.publish_and_verify(
            store,
            plan,
            handle=handle,
            sqlite_path=database,
            mirror=mirror,
            directory=tmp_path / "missing",
        )
    assert len(store.put_keys) == puts
    store.values[key] = saved
    checkpoint = CohortStage(new_result(plan, 0))
    store.store_object(key, stage_bytes(plan, checkpoint))
    with pytest.raises(ValueError, match="all fixed pairs attempted"):
        external_assessment.publish_and_verify(
            store,
            plan,
            handle=handle,
            sqlite_path=database,
            mirror=mirror,
            directory=tmp_path / "pending",
        )


def test_workflow_is_daily_serial_private_and_keeps_publication_inside_existing_lease():
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch", "schedule"}
    assert workflow["concurrency"]["group"] == "external-assessment-acquire"
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    acquire_job = workflow["jobs"]["acquire"]
    assert acquire_job["strategy"]["max-parallel"] == "1"
    assert int(acquire_job["timeout-minutes"]) < 360
    publisher_job = workflow["jobs"]["publish"]
    assert publisher_job["concurrency"]["group"] == "cloud-publish"
    runs = [step.get("run", "") for step in publisher_job["steps"]]
    lease = next(i for i, text in enumerate(runs) if "publication_lease acquire" in text)
    hydrate = next(i for i, text in enumerate(runs) if "hydrate-market" in text)
    publish = next(
        i for i, step in enumerate(publisher_job["steps"]) if step.get("id") == "publish"
    )
    assert lease < hydrate < publish
    assert "--lease-handle" in runs[publish]
    assert 'external_assessment_daily "$publish_command"' in runs[publish]
    assert "publish_command=publish" in runs[publish]
    assert "publish_command=publish-confirmed-partial" in runs[publish]
    assert int(publisher_job["timeout-minutes"]) < 120
    assert "refs/heads/main" in workflow["jobs"]["plan"]["if"]
    assert not any(
        "upload-artifact" in step.get("uses", "")
        for job in workflow["jobs"].values()
        for step in job["steps"]
    )
    assert check_workflow(WORKFLOW) == []


def test_selected_month_history_keeps_old_receipts_and_ignores_other_months(publication):
    fixed, cache, database = fixed_external_assessment_readback(publication)
    selected = {
        (external_assessment.VALUATION_DATASET, 2026, 9),
        (external_assessment.RATING_DATASET, 2026, 9),
    }
    expected = external_assessment_history_digests(database, partitions=selected)
    with lake_session() as session:
        external_assessment_verify_release_history(
            session,
            release=fixed,
            cache=cache,
            expected_history=expected,
            observations=(),
            partitions=selected,
        )
    # An old receipt in the selected month must still match all its metadata.
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "UPDATE external_assessment_model_valuation_observations SET consensus_target_updated_label='changed' WHERE snapshot_date >= '2026-09-01' AND snapshot_date < '2026-10-01'"
        )
    changed = external_assessment_history_digests(database, partitions=selected)
    assert changed != expected
    with lake_session() as session, pytest.raises(ValueError, match="changed received history"):
        external_assessment_verify_release_history(
            session,
            release=fixed,
            cache=cache,
            expected_history=changed,
            observations=(),
            partitions=selected,
        )
    # The August partition is outside this readback scope.
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "DELETE FROM external_assessment_analyst_rating_observations WHERE snapshot_date < '2026-09-01'"
        )
    assert external_assessment_history_digests(database, partitions=selected) == changed


def test_selected_month_missing_old_receipt_is_detected(publication):
    fixed, cache, database = fixed_external_assessment_readback(publication)
    selected = {(external_assessment.VALUATION_DATASET, 2026, 9)}
    expected = external_assessment_history_digests(database, partitions=selected)
    manifests = dict(fixed.dataset_manifests)
    original = manifests[external_assessment.VALUATION_DATASET]
    manifests[external_assessment.VALUATION_DATASET] = original.model_copy(
        update={
            "partitions": tuple(part for part in original.partitions if part.values["month"] != 9)
        }
    )
    with lake_session() as session, pytest.raises(ValueError, match="changed received history"):
        external_assessment_verify_release_history(
            session,
            release=replace(fixed, dataset_manifests=manifests),
            cache=cache,
            expected_history=expected,
            observations=(),
            partitions=selected,
        )


def test_quants_score_optional_l1_publication_and_idempotent_terminal(publication):
    store, previous, handle, database, mirror, directory = publication
    before = external_assessment_history_digests(database)
    p = make_plan(
        run_id="synthetic-quants",
        tickers=("8306",),
        sources=("kabumap",),
        universe_binding=previous.universe_binding,
        collector_binding=external_assessment_collector_binding(),
    )
    responses = pair_responses("kabumap", "8306", received_at_utc=RECEIVED)
    item = external_assessment_decode_attempt(responses)
    assert isinstance(item, ExternalAssessmentQuantsScoreObservation)
    journal = record_outcome(
        p,
        new_result(p, 0),
        ExternalAssessmentCollectionOutcome(
            "kabumap",
            "8306",
            "observed",
            http_status=200,
            attempt_id=responses[0].attempt_id,
        ),
        item,
    )
    bundle_key = upload_bundle(store, p, 0, responses)
    put_private(
        store,
        stage_key(p, 0),
        stage_bytes(p, CohortStage(journal, raw_bundle_keys=(bundle_key,))),
        previous_etag=None,
    )
    report = external_assessment.publish_and_verify(
        store,
        p,
        handle=handle,
        sqlite_path=database,
        mirror=mirror,
        directory=directory / "quants-readback",
    )
    assert report["readback"] == "verified"
    assert report["rows_added"] == 1
    assert report["external_assessment_readback_parquet_objects"] == 2
    external_assessment_verify_observations(database, [item])
    after = external_assessment_history_digests(database)
    assert (
        after["external_assessment_model_valuation_observations"]
        == before["external_assessment_model_valuation_observations"]
    )
    assert (
        after["external_assessment_analyst_rating_observations"]
        == before["external_assessment_analyst_rating_observations"]
    )
    assert (
        after["external_assessment_quants_score_observations"]
        != before["external_assessment_quants_score_observations"]
    )
    repeat = external_assessment.publish_and_verify(
        store,
        p,
        handle=handle,
        sqlite_path=database,
        mirror=mirror,
        directory=directory / "quants-replay",
    )
    assert repeat["rows_added"] == 0
    assert repeat["readback"] == "previously_verified"
