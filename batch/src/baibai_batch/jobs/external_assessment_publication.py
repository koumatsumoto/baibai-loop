"""Publish confirmed stages onto the latest L1 state under the existing lease."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from baibai_batch.jobs.external_assessment_cohorts import RunPlan, summarize
from baibai_batch.storage.external_assessment_identity import (
    PARTIAL_TERMINAL_FORMAT,
    PUBLISHED_FORMAT,
)
from baibai_batch.storage.external_assessment_staging import (
    prefix,
    put_private,
    read_private,
    read_stages,
    read_terminal,
    terminal_status,
)
from baibai_batch.storage.lake_publish import LakePublishError, ObjectStore
from baibai_batch.storage.publication_lease import PublicationLeaseHandle, verify_held
from baibai_batch.storage.publish_market_lake import (
    _fetch,
    _resolve_serving_release,
    _serving_pointer,
    publish_market_lake,
)
from baibai_engine.batch_api import (
    EXTERNAL_ASSESSMENT_ANALYST_RATING_DATASET as ANALYST_RATING_DATASET,
)
from baibai_engine.batch_api import (
    EXTERNAL_ASSESSMENT_MODEL_VALUATION_DATASET as MODEL_VALUATION_DATASET,
)
from baibai_engine.batch_api import (
    EXTERNAL_ASSESSMENT_QUANTS_SCORE_DATASET as QUANTS_SCORE_DATASET,
)
from baibai_engine.batch_api import (
    ExternalAssessmentModelValuationObservation,
    ExternalAssessmentObservation,
    ExternalAssessmentQuantsScoreObservation,
    LakeFixedRelease,
    LakeObjectCache,
    LocalMirrorSource,
    external_assessment_history_digests,
    external_assessment_save_observations,
    external_assessment_verify_release_history,
    lake_session,
    read_lake_store_origin,
)

VALUATION_DATASET = MODEL_VALUATION_DATASET
RATING_DATASET = ANALYST_RATING_DATASET
QUANTS_DATASET = QUANTS_SCORE_DATASET


def receipt_date_coverage(
    plan: RunPlan, observations: Sequence[ExternalAssessmentObservation]
) -> dict[str, dict[str, int]]:
    coverage: dict[str, dict[str, int]] = {}
    for observation in observations:
        sources = coverage.setdefault(
            observation.snapshot_date.isoformat(), dict.fromkeys(plan.sources, 0)
        )
        sources[observation.distributor] += 1
    return dict(sorted(coverage.items()))


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
    confirmed_partial: bool = False,
) -> dict[str, object]:
    """New publication requires the lease; sealed batches return their original receipt."""
    saved_terminal = read_terminal(store, plan)
    if saved_terminal is not None:
        return {
            **saved_terminal,
            "status": terminal_status(saved_terminal),
            "readback": "previously_verified",
            "rows_added": 0,
            "uploaded_bytes": 0,
            "uploaded_objects": 0,
            "publication_transfers": None,
        }
    verify_held(store, handle)
    stages = read_stages(store, plan)
    results = [stage.result for stage, _ in stages.values()]
    if not confirmed_partial and any(
        entry.http_status in {403, 429} for result in results for entry in result.entries
    ):
        raise ValueError(
            "blocked provider response requires explicit confirmed partial publication"
        )
    accounting = summarize(plan, results, require_all=not confirmed_partial)
    if not confirmed_partial and not accounting["attempted_completeness"]:
        raise ValueError("external assessment publication requires all fixed pairs attempted")
    observations = tuple(item for result in results for item in result.observations)
    if confirmed_partial and not observations:
        raise ValueError(
            "external assessment partial publication requires confirmed typed observations"
        )
    coverage = receipt_date_coverage(plan, observations)
    previous = _serving_pointer(store).pointer
    if previous is None:
        raise LakePublishError("external assessment publication requires an existing L1 release")
    before = _resolve_serving_release(store, mirror, previous)
    origin = read_lake_store_origin(sqlite_path)
    if origin is None or (origin.release_id, origin.release_manifest_sha256) != (
        before.release_id,
        before.manifest_sha256,
    ):
        raise LakePublishError("external assessment publication requires a fresh current hydrate")
    directory.mkdir(mode=0o700)
    verify_held(store, handle)
    inserted = external_assessment_save_observations(sqlite_path, observations)
    changed = {
        (
            VALUATION_DATASET
            if isinstance(item, ExternalAssessmentModelValuationObservation)
            else QUANTS_DATASET
            if isinstance(item, ExternalAssessmentQuantsScoreObservation)
            else RATING_DATASET,
            item.snapshot_date.year,
            item.snapshot_date.month,
        )
        for item in observations
    }
    expected_history = external_assessment_history_digests(sqlite_path, partitions=changed)
    # Keep the publisher's verified remote bytes in this attempt's private mirror.
    # The fixed-release reader rechecks their SHA before validating typed history.
    readback_mirror = directory / "mirror"
    readback_mirror.mkdir(mode=0o700)
    verify_held(store, handle)
    publication = (
        publish_market_lake(
            sqlite_path=sqlite_path,
            mirror_root=mirror,
            store=store,
            before_pointer_switch=lambda: verify_held(store, handle),
            verified_readback_root=readback_mirror,
        )
        if inserted
        else None
    )
    current = _serving_pointer(store).pointer
    if current is None or (
        publication is not None
        and (current.release_id, current.manifest_sha256)
        != (publication.release_id, publication.release_manifest_sha256)
    ):
        raise LakePublishError("external assessment publication changed before readback")
    after = _resolve_serving_release(store, readback_mirror, current)
    if set(after.dataset_names()) != set(before.dataset_names()) | {item[0] for item in changed}:
        raise LakePublishError("external assessment publication changed dataset inventory")
    if unchanged_objects(before, changed) != unchanged_objects(after, changed):
        raise LakePublishError("external assessment publication changed unrelated partitions")
    for name in (VALUATION_DATASET, RATING_DATASET, QUANTS_DATASET):
        manifest = after.dataset_manifests.get(name)
        if manifest is None:
            continue
        for part in manifest.partitions:
            if (name, part.values["year"], part.values["month"]) not in changed:
                continue
            for obj in part.objects:
                _fetch(store, readback_mirror, obj.key, expected_sha256=obj.sha256)
    with lake_session() as session:
        external_assessment_verify_release_history(
            session,
            release=after,
            cache=LakeObjectCache(root=readback_mirror, source=LocalMirrorSource(readback_mirror)),
            expected_history=expected_history,
            observations=observations,
            partitions=changed,
        )
    verify_held(store, handle)
    terminal: dict[str, object] = {
        "format": (PARTIAL_TERMINAL_FORMAT if confirmed_partial else PUBLISHED_FORMAT),
        "plan_sha256": plan.plan_sha256,
        "release_id": after.release_id,
        "release_manifest_sha256": after.manifest_sha256,
        "expected_pairs": len(plan.tickers) * len(plan.sources),
        "observations": len(observations),
        "readback": "verified",
    }
    if confirmed_partial:
        terminal["state_counts"] = accounting["state_counts"]
        terminal["receipt_date_coverage"] = coverage
    key = prefix(plan.run_id) + "/published.json"
    saved = read_private(store, key)
    if saved is not None and json.loads(saved.payload)["plan_sha256"] != plan.plan_sha256:
        raise ValueError("external assessment published batch binding mismatch")
    if saved is None or json.loads(saved.payload) != terminal:
        put_private(
            store,
            key,
            json.dumps(terminal, sort_keys=True).encode(),
            previous_etag=saved.etag if saved else None,
        )
    reachable = {
        obj.key: obj.bytes
        for manifest in after.dataset_manifests.values()
        for part in manifest.partitions
        for obj in part.objects
    }
    external_assessment_readback = {
        obj.key: obj.bytes
        for name, manifest in after.dataset_manifests.items()
        if name in {VALUATION_DATASET, RATING_DATASET, QUANTS_DATASET}
        for part in manifest.partitions
        if (name, part.values["year"], part.values["month"]) in changed
        for obj in part.objects
    }
    return {
        **accounting,
        **terminal,
        "status": "partially_published" if confirmed_partial else "published",
        "receipt_date_coverage": coverage,
        "rows_added": inserted,
        "uploaded_bytes": publication.uploaded_bytes if publication else 0,
        "uploaded_objects": publication.uploaded_objects if publication else 0,
        "publication_transfers": publication.transfers.as_dict() if publication else None,
        "publication_transfers_scope": (
            "immutable_graph_and_pointer_only; "
            "excludes lease/stage/base-resolve/hydrate/independent-readback"
        ),
        # Manifest sizes describe the readback scope, not actual transfers/cache hits.
        "current_reachable_expected_parquet_bytes": sum(reachable.values()),
        "external_assessment_readback_expected_parquet_bytes": sum(
            external_assessment_readback.values()
        ),
        "external_assessment_readback_parquet_objects": len(external_assessment_readback),
    }
