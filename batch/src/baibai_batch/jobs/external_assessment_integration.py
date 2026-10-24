"""Merge exact external plans before the one common export, then verify and seal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from baibai_batch.jobs.external_assessment_publication import (
    QUANTS_DATASET,
    RATING_DATASET,
    SOURCE_RESPONSES_DATASET,
    VALUATION_DATASET,
    _import_confirmed_originals,
    receipt_date_coverage,
    unchanged_objects,
)
from baibai_batch.storage.external_assessment_identity import (
    PARTIAL_TERMINAL_FORMAT,
    PUBLISHED_FORMAT,
)
from baibai_batch.storage.external_assessment_ready import load_ready
from baibai_batch.storage.external_assessment_staging import prefix, put_private, read_terminal
from baibai_batch.storage.lake_publish import LakePublishError, ObjectStore
from baibai_batch.storage.publication_lease import PublicationLeaseHandle, verify_held
from baibai_batch.storage.publish_market_lake import (
    _fetch,
    _resolve_serving_release,
    _serving_pointer,
)
from baibai_engine.batch_api import (
    EXTERNAL_ASSESSMENT_ANALYST_FORECAST_DATASET as ANALYST_FORECAST_DATASET,
)
from baibai_engine.batch_api import (
    ExternalAssessmentAnalystForecastObservation,
    ExternalAssessmentModelValuationObservation,
    ExternalAssessmentObservation,
    ExternalAssessmentQuantsScoreObservation,
    LakeObjectCache,
    LakeReleasePointer,
    LocalMirrorSource,
    equity_web_history_digests,
    equity_web_verify_release_history,
    external_assessment_history_digests,
    external_assessment_save_observations,
    external_assessment_verify_release_history,
    lake_release_manifest_key,
    lake_session,
    read_lake_store_origin,
)

_EXCLUSIVE_DATASETS = {
    SOURCE_RESPONSES_DATASET,
    VALUATION_DATASET,
    RATING_DATASET,
    QUANTS_DATASET,
    ANALYST_FORECAST_DATASET,
}


def merge_ready(
    store: ObjectStore,
    references: list[dict[str, str]],
    *,
    database: Path,
) -> dict[str, Any]:
    """Preserve exact plans and each source's failure boundary; never publish here."""
    origin = read_lake_store_origin(database)
    if origin is None:
        raise LakePublishError("common external integration requires a canonical hydrate")
    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    observations: list[ExternalAssessmentObservation] = []
    source_months: set[tuple[int, int]] = set()
    selected_digests: dict[str, str] = {}
    for reference in references:
        retained_months: set[tuple[int, int]] = set()
        retained_digests: dict[str, str] = {}
        try:
            item = load_ready(store, reference)
            if read_terminal(store, item.plan) is not None:
                continue
            added, _, digests, own = _import_confirmed_originals(
                store,
                item.plan,
                item.stages,
                database,
                retained=(retained_months, retained_digests),
            )
            if selected_digests.keys() & digests.keys():
                raise ValueError("receipt identity shared by independent exact plans")
            rows = external_assessment_save_observations(database, own)
        except LakePublishError as error:
            if str(error).startswith("R2 "):
                raise
            failed.append({"reference": reference, "reason": type(error).__name__})
        except (ValueError, KeyError, TypeError) as error:
            failed.append({"reference": reference, "reason": type(error).__name__})
        else:
            observations.extend(own)
            succeeded.append(
                {"reference": reference, "rows_added": rows, "source_responses_added": added}
            )
        finally:
            # Native originals committed before a decode failure are still published/read back.
            source_months.update(retained_months)
            selected_digests.update(retained_digests)
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
    return {
        "before_release_id": origin.release_id,
        "before_manifest_sha256": origin.release_manifest_sha256,
        "plans": succeeded,
        "failed": failed,
        "changed": sorted(changed),
        "history": external_assessment_history_digests(database, partitions=changed),
        "source_history": [
            [year, month, digest]
            for (year, month), digest in sorted(
                equity_web_history_digests(database, months=source_months).items()
            )
        ],
        "selected_digests": selected_digests,
    }


def verify_and_seal(
    store: ObjectStore,
    state: dict[str, Any],
    *,
    handle: PublicationLeaseHandle,
    directory: Path,
) -> list[dict[str, object]]:
    """Read back typed/source history without changing SQLite, then seal each plan."""
    verify_held(store, handle)
    pointer = _serving_pointer(store).pointer
    if pointer is None:
        raise LakePublishError("common external readback requires a canonical release")
    directory.mkdir(parents=True, exist_ok=True)
    after = _resolve_serving_release(store, directory, pointer)
    # Other source datasets legitimately change in common integration. Preserve
    # the external owner's guarantee for its untouched datasets/partitions.
    before = _resolve_serving_release(
        store,
        directory,
        LakeReleasePointer(
            release_id=state["before_release_id"],
            manifest_key=lake_release_manifest_key(release_id=state["before_release_id"]),
            manifest_sha256=state["before_manifest_sha256"],
        ),
    )
    changed = {(str(name), int(year), int(month)) for name, year, month in state["changed"]}
    old = {
        key: value
        for key, value in unchanged_objects(before, changed).items()
        if key in _EXCLUSIVE_DATASETS
    }
    new = {
        key: value
        for key, value in unchanged_objects(after, changed).items()
        if key in _EXCLUSIVE_DATASETS
    }
    if old != new:
        raise LakePublishError(
            "common integration changed unrelated external assessment partitions"
        )
    observations: list[ExternalAssessmentObservation] = []
    prepared = []
    for result in state["plans"]:
        item = load_ready(store, result["reference"])
        prepared.append((item, result))
        observations.extend(
            observation
            for stage, _ in item.stages.values()
            for observation in stage.result.observations
        )
    for name, manifest in after.dataset_manifests.items():
        for part in manifest.partitions:
            if (name, part.values["year"], part.values["month"]) in changed:
                for obj in part.objects:
                    _fetch(store, directory, obj.key, expected_sha256=obj.sha256)
    with lake_session() as session:
        cache = LakeObjectCache(root=directory, source=LocalMirrorSource(directory))
        equity_web_verify_release_history(
            session,
            release=after,
            cache=cache,
            expected_history={
                (int(year), int(month)): digest for year, month, digest in state["source_history"]
            },
            selected_digests=state["selected_digests"],
        )
        external_assessment_verify_release_history(
            session,
            release=after,
            cache=cache,
            expected_history=state["history"],
            observations=observations,
            partitions=changed,
        )
    results = []
    for item, result in prepared:
        verify_held(store, handle)
        plan = item.plan
        own = [
            observation
            for stage, _ in item.stages.values()
            for observation in stage.result.observations
        ]
        terminal: dict[str, object] = {
            "format": PARTIAL_TERMINAL_FORMAT if item.confirmed_partial else PUBLISHED_FORMAT,
            "plan_sha256": plan.plan_sha256,
            "release_id": after.release_id,
            "release_manifest_sha256": after.manifest_sha256,
            "expected_pairs": len(plan.tickers) * len(plan.sources),
            "observations": len(own),
            "readback": "verified",
        }
        if item.confirmed_partial:
            terminal["state_counts"] = item.accounting["state_counts"]
            terminal["receipt_date_coverage"] = receipt_date_coverage(plan, own)
        existing = read_terminal(store, plan)
        if existing is None:
            put_private(
                store,
                prefix(plan.run_id) + "/published.json",
                json.dumps(terminal, sort_keys=True).encode(),
                previous_etag=None,
            )
        results.append({"run_id": plan.run_id, "source": plan.sources[0], **terminal, **result})
    return results
