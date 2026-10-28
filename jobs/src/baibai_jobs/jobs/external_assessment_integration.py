"""Merge exact external plans before the one common export, then verify and seal."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

from baibai_engine.machine_analysis.observations.external_assessments.identity import ANALYST_FORECAST_DATASET
from baibai_engine.machine_analysis.observations.external_assessments.identity import ANALYST_RATING_DATASET as RATING_DATASET
from baibai_engine.machine_analysis.observations.external_assessments.identity import MODEL_VALUATION_DATASET as VALUATION_DATASET
from baibai_engine.machine_analysis.observations.external_assessments.identity import QUANTS_SCORE_DATASET as QUANTS_DATASET
from baibai_engine.machine_analysis.observations.external_assessments.models import AnalystForecastObservation as ExternalAssessmentAnalystForecastObservation
from baibai_engine.machine_analysis.observations.external_assessments.models import ModelValuationObservation as ExternalAssessmentModelValuationObservation
from baibai_engine.machine_analysis.observations.external_assessments.models import Observation as ExternalAssessmentObservation
from baibai_engine.machine_analysis.observations.external_assessments.models import QuantsScoreObservation as ExternalAssessmentQuantsScoreObservation
from baibai_engine.storage.lake.reader import FixedRelease as LakeFixedRelease
from baibai_engine.storage.lake.objects import LakeObjectCache
from baibai_engine.storage.lake.release import LakeReleasePointer
from baibai_engine.storage.lake.objects import LocalMirrorSource
from baibai_engine.storage.lake.equity_web_verification import history_digests as equity_web_history_digests
from baibai_engine.storage.observations.equity_web_import import import_responses as equity_web_import_responses
from baibai_engine.data_collection.equity_web.receipts import read_responses as equity_web_read_responses
from baibai_engine.storage.lake.equity_web_verification import response_digest as equity_web_response_digest
from baibai_engine.storage.lake.equity_web_verification import verify_release_history as equity_web_verify_release_history
from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import decode_attempt as external_assessment_decode_attempt
from baibai_engine.data_collection.external_assessment.verification import history_digests as external_assessment_history_digests
from baibai_engine.machine_analysis.observations.external_assessments.store import save_observations as external_assessment_save_observations
from baibai_engine.data_collection.external_assessment.verification import verify_release_history as external_assessment_verify_release_history
from baibai_engine.storage.lake.keys import release_manifest_key as lake_release_manifest_key
from baibai_engine.storage.lake.duck import lake_session
from baibai_engine.storage.observations.lake_origin import read_lake_store_origin
from baibai_jobs.jobs.external_assessment_cohorts import RunPlan
from baibai_jobs.storage.external_assessment_identity import (
    PARTIAL_TERMINAL_FORMAT,
    PUBLISHED_FORMAT,
)
from baibai_jobs.storage.external_assessment_raw import read_raw_bundle
from baibai_jobs.storage.external_assessment_ready import load_ready
from baibai_jobs.storage.external_assessment_staging import (
    CohortStage,
    prefix,
    put_private,
    read_terminal,
)
from baibai_jobs.storage.lake_publish import LakePublishError, ObjectStore
from baibai_jobs.storage.publication_lease import PublicationLeaseHandle, verify_held
from baibai_jobs.storage.publish_market_lake import (
    _fetch,
    _resolve_serving_release,
    _serving_pointer,
)

SOURCE_RESPONSES_DATASET = "equity_web.source_responses"

_EXCLUSIVE_DATASETS = {
    SOURCE_RESPONSES_DATASET,
    VALUATION_DATASET,
    RATING_DATASET,
    QUANTS_DATASET,
    ANALYST_FORECAST_DATASET,
}


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
    *,
    retained: tuple[set[tuple[int, int]], dict[str, str]] | None = None,
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
            if retained is not None:
                retained[0].update(
                    (item.received_at_utc.year, item.received_at_utc.month) for item in responses
                )
                retained[1].update(
                    (item.receipt_id, equity_web_response_digest(item)) for item in responses
                )
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
    readback_datasets = {name for name, _, _ in changed}
    for name, manifest in after.dataset_manifests.items():
        if name not in readback_datasets:
            continue
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
