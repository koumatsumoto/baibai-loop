"""Publish confirmed stages onto the latest L1 state under the existing lease."""

from __future__ import annotations

import json
from pathlib import Path

from baibai_batch.jobs.retail_cohorts import RunPlan, summarize, validate_resume
from baibai_batch.storage.lake_publish import LakePublishError, ObjectStore
from baibai_batch.storage.publication_lease import PublicationLeaseHandle, verify_held
from baibai_batch.storage.publish_market_lake import (
    _fetch,
    _resolve_serving_release,
    _serving_pointer,
    publish_market_lake,
)
from baibai_batch.storage.retail_staging import prefix, put_private, read_checkpoints, read_private
from baibai_engine.batch_api import (
    LakeFixedRelease,
    LakeObjectCache,
    LocalMirrorSource,
    RetailModelValuationObservation,
    hydrate_market_store,
    lake_session,
    open_market_store,
    read_lake_store_origin,
    retail_history_digests,
    retail_save_observations,
    retail_verify_observations,
)

VALUATION_DATASET = "retail_dynamics.model_valuation_observations"
RATING_DATASET = "retail_dynamics.analyst_rating_observations"


def unchanged_objects(
    release: LakeFixedRelease, changed: set[tuple[str, int, int]]
) -> dict[str, object]:
    return {
        name: [
            obj.model_dump()
            for part in manifest.partitions
            if name not in {item[0] for item in changed}
            or (name, part.values["year"], part.values["month"]) not in changed
            for obj in part.objects
        ]
        for name, manifest in release.dataset_manifests.items()
        if name not in {item[0] for item in changed}
        or any(
            (name, part.values["year"], part.values["month"]) not in changed
            for part in manifest.partitions
        )
    }


def publish_and_verify(
    store: ObjectStore,
    plan: RunPlan,
    *,
    handle: PublicationLeaseHandle,
    sqlite_path: Path,
    mirror: Path,
    directory: Path,
) -> dict[str, object]:
    """Caller holds publication lease across fresh hydrate, this call and readback."""
    verify_held(store, handle)
    checkpoints = read_checkpoints(store, plan)
    journals = [checkpoint.journal for checkpoint, _ in checkpoints.values()]
    validate_resume(plan, journals)
    accounting = summarize(plan, journals, require_all=True)
    if not accounting["attempted_completeness"]:
        raise ValueError("retail publication requires all fixed pairs attempted")
    observations = tuple(item for journal in journals for item in journal.observations)
    previous = _serving_pointer(store).pointer
    if previous is None:
        raise LakePublishError("retail publication requires an existing L1 release")
    before = _resolve_serving_release(store, mirror, previous)
    origin = read_lake_store_origin(sqlite_path)
    if origin is None or (origin.release_id, origin.release_manifest_sha256) != (
        before.release_id,
        before.manifest_sha256,
    ):
        raise LakePublishError("retail publication requires a fresh current hydrate")
    directory.mkdir(mode=0o700)
    verify_held(store, handle)
    inserted = retail_save_observations(sqlite_path, observations)
    expected_history = retail_history_digests(sqlite_path)
    verify_held(store, handle)
    publication = (
        publish_market_lake(sqlite_path=sqlite_path, mirror_root=mirror, store=store)
        if inserted
        else None
    )
    current = _serving_pointer(store).pointer
    if current is None or (
        publication is not None
        and (current.release_id, current.manifest_sha256)
        != (publication.release_id, publication.release_manifest_sha256)
    ):
        raise LakePublishError("retail publication changed before readback")
    readback_mirror = directory / "mirror"
    readback_mirror.mkdir(mode=0o700)
    after = _resolve_serving_release(store, readback_mirror, current)
    changed = {
        (
            VALUATION_DATASET
            if isinstance(item, RetailModelValuationObservation)
            else RATING_DATASET,
            item.snapshot_date.year,
            item.snapshot_date.month,
        )
        for item in observations
    }
    if set(after.dataset_names()) != set(before.dataset_names()) | {item[0] for item in changed}:
        raise LakePublishError("retail publication changed dataset inventory")
    if unchanged_objects(before, changed) != unchanged_objects(after, changed):
        raise LakePublishError("retail publication changed unrelated partitions")
    for manifest in after.dataset_manifests.values():
        for part in manifest.partitions:
            for obj in part.objects:
                _fetch(store, readback_mirror, obj.key, expected_sha256=obj.sha256)
    database = directory / "readback.sqlite"
    open_market_store(database).close()
    database.chmod(0o600)
    with lake_session() as session:
        hydrate_market_store(
            session,
            release=after,
            cache=LakeObjectCache(root=readback_mirror, source=LocalMirrorSource(readback_mirror)),
            store=database,
            dataset_names=tuple(after.dataset_names()),
        )
    retail_verify_observations(database, observations)
    if retail_history_digests(database) != expected_history:
        raise LakePublishError("retail publication changed received history")
    verify_held(store, handle)
    terminal = {
        "format": "retail-published-batch-v1",
        "plan_sha256": plan.plan_sha256,
        "release_id": after.release_id,
        "release_manifest_sha256": after.manifest_sha256,
        "expected_pairs": len(plan.tickers) * len(plan.sources),
        "observations": len(observations),
        "readback": "verified",
    }
    key = prefix(plan.run_id) + "/published.json"
    saved = read_private(store, key)
    if saved is not None and json.loads(saved.payload)["plan_sha256"] != plan.plan_sha256:
        raise ValueError("retail published batch binding mismatch")
    if saved is None or json.loads(saved.payload) != terminal:
        put_private(
            store,
            key,
            json.dumps(terminal, sort_keys=True).encode(),
            previous_etag=saved.etag if saved else None,
        )
    return {
        **accounting,
        **terminal,
        "rows_added": inserted,
        "uploaded_bytes": publication.uploaded_bytes if publication else 0,
        "uploaded_objects": publication.uploaded_objects if publication else 0,
    }
