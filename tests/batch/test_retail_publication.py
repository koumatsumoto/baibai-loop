from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from tests.helpers.l1_release import market_store
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.r2_store import MemoryR2Store
from tools.quality.drift.check_workflow_trust import check_workflow

from baibai_batch.jobs import retail_publication as retail
from baibai_batch.jobs.retail_cohorts import make_plan, mark_inflight, new_journal, record_outcome
from baibai_batch.storage import publish_market_lake as publisher
from baibai_batch.storage.lake_publish import LakeCASConflict, LakePublishError
from baibai_batch.storage.publication_lease import LeaseError, acquire, release, verify_held
from baibai_batch.storage.retail_staging import (
    Checkpoint,
    checkpoint_bytes,
    checkpoint_key,
    put_private,
    read_checkpoints,
)
from baibai_engine.batch_api import (
    RetailAnalystRatingObservation,
    RetailCollectionOutcome,
    RetailModelValuationObservation,
    retail_collector_binding,
    retail_history_digests,
    retail_save_observations,
    retail_verify_observations,
)

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/cloud-retail-dynamics.yml"
RECEIVED = datetime(2026, 9, 30, 15, tzinfo=UTC)


@pytest.fixture
def publication(tmp_path, monkeypatch):
    datasets = (
        "jquants.master_snapshots",
        "jquants.daily_bars",
        "jquants.short_sale_reports",
        retail.VALUATION_DATASET,
        retail.RATING_DATASET,
    )
    narrow_release_policy(monkeypatch, datasets=datasets, minimum_population_count=None)
    monkeypatch.setattr(publisher, "lake_verified_git_commit", lambda: "a" * 40)
    database = market_store(tmp_path / "market.sqlite")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO jquants_master_snapshots VALUES (?,?,?,?,?,?)",
            ("2026-09-30", "7203", "合成会社", "PRIME", "輸送用機器", 1),
        )
    common = {
        "ticker": "7203",
        "fetched_at_utc": RECEIVED - timedelta(days=1),
        "source_url": "https://example.test/synthetic",
        "parser_version": "synthetic-1",
    }
    retail_save_observations(
        database,
        [
            RetailModelValuationObservation(**common, distributor="monex", consensus_target=91),
            RetailAnalystRatingObservation(**common, distributor="rakuten", rating=2),
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
        run_id="synthetic-daily",
        tickers=("7203", "6758"),
        universe_binding="synthetic-fixed-master",
        collector_binding=retail_collector_binding(),
    )
    journal = new_journal(plan, 0)
    for source, ticker in plan.cohorts[0].pairs:
        journal = mark_inflight(plan, journal, source, ticker)
        common = {
            "ticker": ticker,
            "fetched_at_utc": RECEIVED,
            "source_url": "https://example.test/synthetic",
            "parser_version": "synthetic-1",
        }
        if source == "monex":
            observation = RetailModelValuationObservation(
                **common,
                distributor="monex",
                consensus_target=123456.0 if ticker == "7203" else None,
            )
        elif ticker == "7203":
            observation = RetailAnalystRatingObservation(**common, distributor="rakuten", rating=3)
        else:
            observation = None
        outcome = RetailCollectionOutcome(
            source,
            ticker,
            "observed" if observation else "parse_failure",
            None if observation else "no_estimates_identity_unconfirmed",
            200,
        )
        journal = record_outcome(plan, journal, outcome, observation)
    put_private(
        store,
        checkpoint_key(plan, 0),
        checkpoint_bytes(plan, Checkpoint(journal)),
        previous_etag=None,
    )
    return store, plan, handle, database, mirror, tmp_path


def test_real_remote_graph_readback_preserves_null_receipts_history_and_unrelated_data(
    publication,
    capsys,
):
    store, plan, handle, database, mirror, tmp_path = publication
    before = publisher._serving_pointer(store).pointer
    original = store.get_bytes(checkpoint_key(plan, 0))
    report = retail.publish_and_verify(
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
    assert report["release_id"] != before.release_id
    assert store.get_bytes(checkpoint_key(plan, 0)) == original
    assert retail_history_digests(tmp_path / "readback/readback.sqlite") == retail_history_digests(
        database
    )
    assert "123456" not in capsys.readouterr().out
    count = len(store.put_keys)
    repeat = retail.publish_and_verify(
        store,
        plan,
        handle=handle,
        sqlite_path=database,
        mirror=mirror,
        directory=tmp_path / "repeat-readback",
    )
    assert repeat["rows_added"] == repeat["uploaded_bytes"] == repeat["uploaded_objects"] == 0
    assert len(store.put_keys) == count
    retail_verify_observations(
        database,
        [
            entry.observation
            for entry in read_checkpoints(store, plan)[0][0].journal.entries
            if entry.observation is not None
        ],
    )


def test_stale_origin_is_rejected_before_stage_changes_or_canonical_upload(publication):
    store, plan, handle, database, mirror, tmp_path = publication
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE lake_store_origin SET release_id='stale-generation'")
    before = retail_history_digests(database)
    puts = len(store.put_keys)
    with pytest.raises(LakePublishError, match="fresh current hydrate"):
        retail.publish_and_verify(
            store,
            plan,
            handle=handle,
            sqlite_path=database,
            mirror=mirror,
            directory=tmp_path / "rejected",
        )
    assert retail_history_digests(database) == before
    assert len(store.put_keys) == puts


def test_pointer_conflict_keeps_canonical_generation_and_does_not_mark_published(publication):
    store, plan, handle, database, mirror, tmp_path = publication
    before = publisher._serving_pointer(store).pointer
    store.conflict_pointer = True
    with pytest.raises(LakeCASConflict):
        retail.publish_and_verify(
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
        retail.publish_and_verify(
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
    key = checkpoint_key(plan, 0)
    saved = store.values.pop(key)
    puts = len(store.put_keys)
    with pytest.raises(ValueError, match="missing cohort"):
        retail.publish_and_verify(
            store,
            plan,
            handle=handle,
            sqlite_path=database,
            mirror=mirror,
            directory=tmp_path / "missing",
        )
    assert len(store.put_keys) == puts
    store.values[key] = saved
    checkpoint = Checkpoint(new_journal(plan, 0))
    store.store_object(key, checkpoint_bytes(plan, checkpoint))
    with pytest.raises(ValueError, match="all fixed pairs attempted"):
        retail.publish_and_verify(
            store,
            plan,
            handle=handle,
            sqlite_path=database,
            mirror=mirror,
            directory=tmp_path / "pending",
        )


def test_workflow_is_manual_serial_private_and_keeps_publication_inside_existing_lease():
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["concurrency"]["group"] == "retail-dynamics-acquire"
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    acquire_job = workflow["jobs"]["acquire"]
    assert acquire_job["strategy"]["max-parallel"] == "1"
    assert int(acquire_job["timeout-minutes"]) < 360
    publisher_job = workflow["jobs"]["publish"]
    assert publisher_job["concurrency"]["group"] == "cloud-publish"
    runs = [step.get("run", "") for step in publisher_job["steps"]]
    lease = next(i for i, text in enumerate(runs) if "publication_lease acquire" in text)
    hydrate = next(i for i, text in enumerate(runs) if "hydrate-market" in text)
    publish = next(i for i, text in enumerate(runs) if "retail_daily publish" in text)
    assert lease < hydrate < publish
    assert "--lease-handle" in runs[publish]
    assert int(publisher_job["timeout-minutes"]) < 120
    assert "refs/heads/main" in workflow["jobs"]["plan"]["if"]
    assert not any(
        "upload-artifact" in step.get("uses", "")
        for job in workflow["jobs"].values()
        for step in job["steps"]
    )
    assert check_workflow(WORKFLOW) == []
