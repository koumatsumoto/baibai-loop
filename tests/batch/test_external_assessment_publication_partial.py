from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from tests.batch.external_assessment_publication_fixtures import pair_responses, upload_bundle
from tests.batch.test_external_assessment_publication import RECEIVED
from tests.batch.test_external_assessment_publication import publication as publication

from baibai_batch.jobs import external_assessment_publication as external_assessment
from baibai_batch.jobs.external_assessment_cohorts import (
    make_plan,
    new_result,
    record_outcome,
)
from baibai_batch.storage.external_assessment_staging import (
    CohortStage,
    prefix,
    read_terminal,
    stage_bytes,
    stage_key,
)
from baibai_batch.storage.lake_publish import LakePublishError
from baibai_batch.storage.publication_lease import LeaseError, release
from baibai_engine.batch_api import (
    ExternalAssessmentCollectionOutcome,
    external_assessment_decode_attempt,
    external_assessment_history_digests,
)
from baibai_engine.external_assessment.acquisition.verification import (
    verify_observations as external_assessment_verify_observations,
)


@pytest.fixture
def partial_batch(publication):
    store, previous_plan, handle, database, mirror, directory = publication
    plan = make_plan(
        run_id="synthetic-partial",
        tickers=("130A", "6758", "7203"),
        universe_binding=previous_plan.universe_binding,
        collector_binding=previous_plan.collector_binding,
        sources=("monex", "rakuten"),
        cohort_size=1,
    )
    monex = pair_responses(
        "monex", "130A", received_at_utc=RECEIVED - timedelta(microseconds=1), value="0円"
    )
    rakuten = pair_responses("rakuten", "130A", received_at_utc=RECEIVED, value="--")
    observations = (
        external_assessment_decode_attempt(monex),
        external_assessment_decode_attempt(rakuten),
    )
    confirmed = new_result(plan, 0)
    for observation, responses in zip(observations, (monex, rakuten), strict=True):
        confirmed = record_outcome(
            plan,
            confirmed,
            ExternalAssessmentCollectionOutcome(
                observation.distributor,
                "130A",
                "observed",
                None,
                200,
                responses[0].attempt_id,
            ),
            observation,
        )
    halted = new_result(plan, 1)
    malformed = pair_responses("monex", "6758", received_at_utc=RECEIVED, malformed=True)
    halted = record_outcome(
        plan,
        halted,
        ExternalAssessmentCollectionOutcome(
            "monex",
            "6758",
            "parse_failure",
            "missing_theory_table",
            200,
            malformed[0].attempt_id,
        ),
    )
    for checkpoint in (
        CohortStage(
            confirmed,
            http_requests=3,
            raw_bundle_keys=(upload_bundle(store, plan, 0, (*monex, *rakuten)),),
        ),
        CohortStage(
            halted,
            http_requests=1,
            raw_bundle_keys=(upload_bundle(store, plan, 1, malformed),),
        ),
    ):
        store.store_object(
            stage_key(plan, checkpoint.result.cohort_index),
            stage_bytes(plan, checkpoint),
        )
    # Cohort 2 was never entered; partial publication must not manufacture it.
    return store, plan, handle, database, mirror, directory, observations


def checkpoint_payloads(store):
    return {key: value.body for key, value in store.values.items() if "/cohort-" in key}


def publish_partial(batch, *, confirmed_partial=True, directory_name="partial-readback"):
    store, plan, handle, database, mirror, directory, _ = batch
    return external_assessment.publish_and_verify(
        store,
        plan,
        handle=handle,
        sqlite_path=database,
        mirror=mirror,
        directory=directory / directory_name,
        confirmed_partial=confirmed_partial,
    )


def test_explicit_partial_publishes_only_confirmed_receipts_without_changing_halted_stage(
    partial_batch,
):
    store, plan, _, database, _, _, observations = partial_batch
    original = checkpoint_payloads(store)
    history = external_assessment_history_digests(database)
    puts = len(store.put_keys)
    with pytest.raises(ValueError, match=r"missing cohort|blocked provider"):
        publish_partial(partial_batch, confirmed_partial=False)
    assert external_assessment_history_digests(database) == history
    assert len(store.put_keys) == puts

    report = publish_partial(partial_batch)
    expected_states = {
        "success": 1,
        "normal_null": 1,
        "failure": 1,
        "unattempted": 3,
    }
    expected_coverage = {
        "2026-09-30": {"monex": 1, "rakuten": 0},
        "2026-10-01": {"monex": 0, "rakuten": 1},
    }
    assert report["status"] == "partially_published"
    assert report["readback"] == "verified"
    assert report["format"] == "external-assessment-partially-published-batch-v1"
    assert report["observations"] == report["rows_added"] == 2
    assert report["expected_pairs"] == 6
    assert report["state_counts"] == expected_states
    assert report["receipt_date_coverage"] == expected_coverage
    assert report["whole_universe_accounting"] is False
    assert report["attempted_completeness"] is False
    assert report["success_completeness"] is False
    assert report["missing_cohorts"] == 1
    assert report["unrecorded_pairs"] == 2
    assert report["reason_counts"]["missing_theory_table"] == 1
    assert checkpoint_payloads(store) == original
    assert stage_key(plan, 2) not in store.values
    external_assessment_verify_observations(database, observations)
    terminal = read_terminal(store, plan)
    assert terminal == {
        "format": report["format"],
        "plan_sha256": plan.plan_sha256,
        "release_id": report["release_id"],
        "release_manifest_sha256": report["release_manifest_sha256"],
        "expected_pairs": 6,
        "observations": 2,
        "readback": "verified",
        "state_counts": expected_states,
        "receipt_date_coverage": expected_coverage,
    }


@pytest.mark.parametrize(("reason", "status"), [("access_denied", 403), ("rate_limited", 429)])
def test_explicit_partial_keeps_global_access_halt_and_never_attempted_pairs(
    partial_batch, reason, status
):
    store, plan, _, _, _, _, _ = partial_batch
    journal = new_result(plan, 1)
    journal = record_outcome(
        plan,
        journal,
        ExternalAssessmentCollectionOutcome(
            "monex", "6758", reason, None, status, str(uuid.uuid4())
        ),
    )
    store.store_object(
        stage_key(plan, 1),
        stage_bytes(plan, CohortStage(journal, http_requests=1)),
    )
    original = checkpoint_payloads(store)
    with pytest.raises(ValueError, match=r"missing cohort|blocked provider"):
        publish_partial(partial_batch, confirmed_partial=False)
    report = publish_partial(partial_batch)
    assert report["rows_added"] == report["observations"] == 2
    assert report["state_counts"] == {
        "success": 1,
        "normal_null": 1,
        "failure": 1,
        "unattempted": 3,
    }
    assert report["reason_counts"][reason] == 1
    assert checkpoint_payloads(store) == original
    assert stage_key(plan, 2) not in store.values


def test_partial_requires_at_least_one_confirmed_typed_observation(partial_batch):
    store, plan, _, database, _, directory, _ = partial_batch
    del store.values[stage_key(plan, 0)]
    original = checkpoint_payloads(store)
    history = external_assessment_history_digests(database)
    puts = len(store.put_keys)
    with pytest.raises(ValueError, match="confirmed typed observations"):
        publish_partial(partial_batch)
    assert checkpoint_payloads(store) == original
    assert external_assessment_history_digests(database) == history
    assert len(store.put_keys) == puts
    assert not (directory / "partial-readback").exists()
    assert read_terminal(store, plan) is None


@pytest.mark.parametrize("corruption", ["binding", "duplicate_pair", "observation_identity"])
def test_partial_still_validates_fixed_binding_and_exact_pair_payload(partial_batch, corruption):
    store, plan, _, database, _, _, _ = partial_batch
    key = stage_key(plan, 0)
    checkpoint = json.loads(store.values[key].body)
    if corruption == "binding":
        checkpoint["result"]["plan_sha256"] = "a" * 64
    elif corruption == "duplicate_pair":
        checkpoint["result"]["entries"][1] = checkpoint["result"]["entries"][0]
    else:
        checkpoint["result"]["entries"][0]["observation"]["ticker"] = "6758"
    store.store_object(key, json.dumps(checkpoint).encode())
    history = external_assessment_history_digests(database)
    original = checkpoint_payloads(store)
    puts = len(store.put_keys)
    with pytest.raises(ValueError, match=r"binding|pair|cohort"):
        publish_partial(partial_batch)
    assert external_assessment_history_digests(database) == history
    assert checkpoint_payloads(store) == original
    assert len(store.put_keys) == puts
    assert read_terminal(store, plan) is None


def test_partial_still_requires_the_existing_publication_lease(partial_batch):
    store, plan, handle, database, _, _, _ = partial_batch
    release(store, handle)
    history = external_assessment_history_digests(database)
    original = checkpoint_payloads(store)
    puts = len(store.put_keys)
    with pytest.raises(LeaseError):
        publish_partial(partial_batch)
    assert external_assessment_history_digests(database) == history
    assert checkpoint_payloads(store) == original
    assert len(store.put_keys) == puts
    assert read_terminal(store, plan) is None


def test_partial_independent_readback_failure_cannot_seal_the_batch(partial_batch, monkeypatch):
    store, plan, _, _, _, root, _ = partial_batch
    original = checkpoint_payloads(store)
    download = store.download_file

    def corrupt_readback(key, path, *, expect_bytes=None):
        download(key, path, expect_bytes=expect_bytes)
        if key.endswith(".parquet") and path.is_relative_to(root / "partial-readback"):
            path.write_bytes(b"synthetic-corrupt-readback")

    monkeypatch.setattr(store, "download_file", corrupt_readback)
    with pytest.raises(LakePublishError, match="bytes postcondition failed"):
        publish_partial(partial_batch)
    assert checkpoint_payloads(store) == original
    assert read_terminal(store, plan) is None


@pytest.mark.parametrize("partial", [False, True])
def test_published_receipt_short_circuits_without_checkpoints_lease_db_or_current_release(
    publication, partial_batch, monkeypatch, partial
):
    if partial:
        first = publish_partial(partial_batch)
        store, plan, handle, _, _, root, _ = partial_batch
    else:
        store, plan, handle, database, mirror, root = publication
        first = external_assessment.publish_and_verify(
            store,
            plan,
            handle=handle,
            sqlite_path=database,
            mirror=mirror,
            directory=root / "full-readback",
        )
        assert first["receipt_date_coverage"] == {"2026-10-01": {"monex": 2, "rakuten": 1}}
    key = prefix(plan.run_id) + "/published.json"
    receipt = store.values[key].body
    release(store, handle)

    def forbidden(*args, **kwargs):
        raise AssertionError("a sealed batch must return its original receipt without rereading")

    for name in (
        "read_stages",
        "verify_held",
        "_serving_pointer",
        "_resolve_serving_release",
        "read_lake_store_origin",
        "external_assessment_save_observations",
        "external_assessment_history_digests",
        "publish_market_lake",
        "lake_session",
    ):
        monkeypatch.setattr(external_assessment, name, forbidden)
    store.get_keys.clear()
    store.head_keys.clear()
    store.put_keys.clear()
    repeat = external_assessment.publish_and_verify(
        store,
        plan,
        handle=handle,
        sqlite_path=root / "does-not-exist.sqlite",
        mirror=root / "does-not-exist-mirror",
        directory=root / "must-not-create",
        confirmed_partial=not partial,
    )
    assert repeat["status"] == ("already_partially_published" if partial else "already_published")
    assert repeat["readback"] == "previously_verified"
    assert repeat["release_id"] == first["release_id"]
    assert repeat["release_manifest_sha256"] == first["release_manifest_sha256"]
    assert repeat["rows_added"] == repeat["uploaded_bytes"] == repeat["uploaded_objects"] == 0
    assert repeat["publication_transfers"] is None
    if partial:
        assert repeat["state_counts"] == first["state_counts"]
        assert repeat["receipt_date_coverage"] == first["receipt_date_coverage"]
    else:
        assert "state_counts" not in repeat
        assert "receipt_date_coverage" not in repeat
    assert store.values[key].body == receipt
    assert store.get_keys == [key]
    assert store.head_keys == [key, key]
    assert store.put_keys == []
    assert not (root / "must-not-create").exists()
