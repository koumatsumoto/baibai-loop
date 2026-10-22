"""Publish confirmed stages onto the latest L1 state under the existing lease."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from baibai_batch.jobs.external_assessment_cohorts import RunPlan, summarize
from baibai_batch.storage.external_assessment_identity import (
    PARTIAL_TERMINAL_FORMAT,
    PUBLISHED_FORMAT,
)
from baibai_batch.storage.external_assessment_raw import read_raw_bundle
from baibai_batch.storage.external_assessment_staging import (
    CohortStage,
    prefix,
    put_private,
    read_private,
    read_stages,
    read_terminal,
    terminal_result,
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
    EXTERNAL_ASSESSMENT_ANALYST_FORECAST_DATASET as ANALYST_FORECAST_DATASET,
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
    ExternalAssessmentAnalystForecastObservation,
    ExternalAssessmentModelValuationObservation,
    ExternalAssessmentObservation,
    ExternalAssessmentQuantsScoreObservation,
    LakeFixedRelease,
    LakeObjectCache,
    LocalMirrorSource,
    equity_web_history_digests,
    equity_web_import_responses,
    equity_web_read_responses,
    equity_web_response_digest,
    equity_web_verify_release_history,
    external_assessment_decode_attempt,
    external_assessment_history_digests,
    external_assessment_save_observations,
    external_assessment_verify_release_history,
    lake_session,
    read_lake_store_origin,
)

VALUATION_DATASET = MODEL_VALUATION_DATASET
RATING_DATASET = ANALYST_RATING_DATASET
QUANTS_DATASET = QUANTS_SCORE_DATASET
SOURCE_RESPONSES_DATASET = "equity_web.source_responses"


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
    changed_datasets = {name for name, _, _ in changed}
    return {
        name: [
            obj.model_dump()
            for part in manifest.partitions
            if name not in changed_datasets
            or (name, part.values["year"], part.values["month"]) not in changed
            for obj in part.objects
        ]
        for name, manifest in release.dataset_manifests.items()
        if name not in changed_datasets
        or any(
            (name, part.values["year"], part.values["month"]) not in changed
            for part in manifest.partitions
        )
    }


def _import_confirmed_originals(
    store: ObjectStore,
    plan: RunPlan,
    stages: dict[int, tuple[CohortStage, str]],
    sqlite_path: Path,
) -> tuple[int, set[tuple[int, int]], dict[str, str], tuple[ExternalAssessmentObservation, ...]]:
    """Import one bounded bundle at a time; defer L2 until every bundle is valid."""
    source_inserted = 0
    source_months: set[tuple[int, int]] = set()
    selected_digests: dict[str, str] = {}
    typed: list[ExternalAssessmentObservation] = []
    attempts: dict[str, tuple[str, str, list[str]]] = {}
    for index, (stage, _) in sorted(stages.items()):
        pairs = set(plan.cohorts[index].pairs)
        for key in stage.raw_bundle_keys:
            manifest, responses = read_raw_bundle(store, key)
            if manifest["batch_id"] != plan.run_id or manifest["cohort_index"] != index:
                raise ValueError("external assessment raw bundle stage binding mismatch")
            for response in responses:
                if (response.source, response.requested_ticker) not in pairs or (
                    response.receipt_id in selected_digests
                ):
                    raise ValueError("external assessment raw receipt does not belong to cohort")
                previous = attempts.get(response.attempt_id)
                if previous is None:
                    attempts[response.attempt_id] = (
                        response.source,
                        response.requested_ticker,
                        [response.receipt_id],
                    )
                elif previous[:2] != (response.source, response.requested_ticker):
                    raise ValueError("external assessment raw attempt identity mismatch")
                else:
                    previous[2].append(response.receipt_id)
                selected_digests[response.receipt_id] = equity_web_response_digest(response)
                source_months.add((response.received_at_utc.year, response.received_at_utc.month))
            source_inserted += equity_web_import_responses(sqlite_path, responses)
            # Responses and their decompressed bodies die with this bundle iteration.
            del responses
        for entry in stage.result.entries:
            attempt_info = attempts.get(entry.attempt_id) if entry.attempt_id else None
            if entry.state in {"success", "normal_null"}:
                if attempt_info is None or attempt_info[:2] != (entry.source, entry.ticker):
                    raise ValueError(
                        "selected external assessment attempt has no matching original"
                    )
                uri = sqlite_path.resolve().as_uri() + "?mode=ro"
                with closing(sqlite3.connect(uri, uri=True)) as conn:
                    attempt = equity_web_read_responses(conn, attempt_info[2])
                parsed = external_assessment_decode_attempt(attempt)
                if parsed != entry.observation:
                    raise ValueError("external assessment staged observation differs from original")
                typed.append(parsed)
            elif (
                entry.state == "failure"
                and entry.http_status == 200
                and (attempt_info is None or attempt_info[:2] != (entry.source, entry.ticker))
            ):
                raise ValueError("failed HTTP 200 attempt has no retained original")
    return source_inserted, source_months, selected_digests, tuple(typed)


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
        return terminal_result(saved_terminal)
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
    staged_observations = tuple(item for result in results for item in result.observations)
    if confirmed_partial and not staged_observations:
        raise ValueError(
            "external assessment partial publication requires confirmed typed observations"
        )
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
    source_inserted, source_months, selected_digests, observations = _import_confirmed_originals(
        store, plan, stages, sqlite_path
    )
    coverage = receipt_date_coverage(plan, observations)
    inserted = external_assessment_save_observations(sqlite_path, observations)
    changed = {
        (
            VALUATION_DATASET
            if isinstance(item, ExternalAssessmentModelValuationObservation)
            else QUANTS_DATASET
            if isinstance(item, ExternalAssessmentQuantsScoreObservation)
            else ANALYST_FORECAST_DATASET
            if isinstance(item, ExternalAssessmentAnalystForecastObservation)
            else RATING_DATASET,
            item.snapshot_date.year,
            item.snapshot_date.month,
        )
        for item in observations
    }
    changed.update((SOURCE_RESPONSES_DATASET, year, month) for year, month in source_months)
    expected_history = external_assessment_history_digests(sqlite_path, partitions=changed)
    expected_source_history = equity_web_history_digests(sqlite_path, months=source_months)
    # Keep the publisher's verified remote bytes in this attempt's private mirror.
    # The fixed-release reader rechecks their SHA before validating typed history.
    readback_mirror = directory / "mirror"
    readback_mirror.mkdir(mode=0o700)
    verify_held(store, handle)
    # A failed prior attempt may have imported every original locally before
    # publication; retry must still publish those already-retained receipts.
    publication = (
        publish_market_lake(
            sqlite_path=sqlite_path,
            mirror_root=mirror,
            store=store,
            before_pointer_switch=lambda: verify_held(store, handle),
            verified_readback_root=readback_mirror,
        )
        if source_inserted or inserted or selected_digests
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
    for name in (SOURCE_RESPONSES_DATASET, VALUATION_DATASET, RATING_DATASET, QUANTS_DATASET):
        manifest = after.dataset_manifests.get(name)
        if manifest is None:
            continue
        for part in manifest.partitions:
            if (name, part.values["year"], part.values["month"]) not in changed:
                continue
            for obj in part.objects:
                _fetch(store, readback_mirror, obj.key, expected_sha256=obj.sha256)
    with lake_session() as session:
        cache = LakeObjectCache(root=readback_mirror, source=LocalMirrorSource(readback_mirror))
        equity_web_verify_release_history(
            session,
            release=after,
            cache=cache,
            expected_history=expected_source_history,
            selected_digests=selected_digests,
        )
        external_assessment_verify_release_history(
            session,
            release=after,
            cache=cache,
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
        if name in {SOURCE_RESPONSES_DATASET, VALUATION_DATASET, RATING_DATASET, QUANTS_DATASET}
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
        "source_responses_added": source_inserted,
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
