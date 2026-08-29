"""Publish the first market v25 L1 release from an exact serving v24 release.

The v24 immutable manifests contain audit-only fields that the current contract no
longer accepts. This one-shot operator keeps their digest and retained history-floor
checks, publishes only the current dataset inventory, and advances the market store
origin through the normal compare-and-swap publication path.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from baibai_batch.storage.lake_publish import (
    Boto3R2Store,
    LakePublishError,
    ObjectStore,
    read_pointer_snapshot,
)
from baibai_batch.storage.publish_market_lake import (
    _fetch,
    _ServingCoverageOverride,
    publish_market_lake,
)
from baibai_engine.batch_api import (
    LAKE_DATASETS,
    L1ReleasePointer,
    LakeBuildError,
    LakeDatasetManifest,
    LakeReleaseManifest,
    LakeStoreOrigin,
    lake_dataset_manifest_key,
    load_lake_model_json,
)

_RETIRED_DATASET = "edinet.buyback_reports"
_OLD_EARNINGS_DATASET = "jquants.earnings_calendar"
_CURRENT_EARNINGS_DATASET = "jpx.earnings_calendar"
_NEW_DATASETS = frozenset({"jquants.all_issues_daily_margin"})


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _load_old_dataset_manifest(raw: bytes) -> LakeDatasetManifest:
    """Validate one exact old manifest after removing its retired audit fields."""
    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        if not isinstance(payload, dict):
            raise ValueError("manifest root is not an object")
        for field in ("cohort_inventory", "transform_fingerprint"):
            if field not in payload:
                raise ValueError(f"old manifest is missing {field}")
            del payload[field]
        partitions = payload.get("partitions")
        if not isinstance(partitions, list) or not partitions:
            raise ValueError("old manifest has no partitions")
        for partition in partitions:
            if not isinstance(partition, dict) or "source_state_sha256" not in partition:
                raise ValueError("old partition is missing source_state_sha256")
            del partition["source_state_sha256"]
        return load_lake_model_json(
            json.dumps(payload, separators=(",", ":")).encode(), LakeDatasetManifest
        )
    except (TypeError, ValueError) as exc:
        raise LakePublishError("old dataset manifest is invalid") from exc


def _resolve_old_coverage_starts(
    store: ObjectStore,
    mirror_root: Path,
    serving: L1ReleasePointer,
) -> Mapping[str, date]:
    """Read only the retained history floors from the exact old release inventory."""
    release_path = _fetch(
        store,
        mirror_root,
        serving.manifest_key,
        expected_sha256=serving.manifest_sha256,
    )
    try:
        release = load_lake_model_json(release_path.read_bytes(), LakeReleaseManifest)
    except ValueError:
        raise LakePublishError(
            f"serving release manifest is invalid: {serving.manifest_key}"
        ) from None
    if release.release_id != serving.release_id:
        raise LakePublishError("old serving release identifies a different release")

    current_names = set(LAKE_DATASETS)
    expected_old_names = (current_names - {_CURRENT_EARNINGS_DATASET} - _NEW_DATASETS) | {
        _OLD_EARNINGS_DATASET,
        _RETIRED_DATASET,
    }
    if set(release.datasets) != expected_old_names:
        raise LakePublishError("serving release is not the exact old dataset inventory")

    coverage_starts: dict[str, date] = {}
    for current_name in sorted(current_names - _NEW_DATASETS):
        old_name = (
            _OLD_EARNINGS_DATASET if current_name == _CURRENT_EARNINGS_DATASET else current_name
        )
        entry = release.datasets[old_name]
        manifest_path = _fetch(
            store,
            mirror_root,
            lake_dataset_manifest_key(dataset=old_name, build_id=entry.build_id),
            expected_sha256=entry.manifest_sha256,
        )
        manifest = _load_old_dataset_manifest(manifest_path.read_bytes())
        if (
            manifest.dataset != old_name
            or manifest.build_id != entry.build_id
            or manifest.contract_version != entry.contract_version
            or manifest.data_as_of != entry.data_as_of
            or manifest.coverage_status != entry.coverage_status
            or manifest.totals != entry.totals
            or manifest.layer != "l1_canonical"
        ):
            raise LakePublishError(f"old release and dataset manifest disagree: {old_name}")
        coverage_starts[current_name] = manifest.coverage_start
    return coverage_starts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, required=True)
    parser.add_argument("--mirror", type=Path, required=True)
    parser.add_argument("--bucket", default="baibai-stores")
    args = parser.parse_args()
    try:
        store = Boto3R2Store(bucket=args.bucket)
        serving = read_pointer_snapshot(store).pointer
        if serving is None:
            raise LakePublishError("market v25 cutover requires a serving v24 release")
        coverage_starts = _resolve_old_coverage_starts(store, args.mirror, serving)
        report = publish_market_lake(
            sqlite_path=args.sqlite,
            mirror_root=args.mirror,
            store=store,
            serving_coverage_override=_ServingCoverageOverride(
                origin=LakeStoreOrigin(
                    release_id=serving.release_id,
                    release_manifest_sha256=serving.manifest_sha256,
                ),
                coverage_starts=coverage_starts,
            ),
        )
    except (LakeBuildError, LakePublishError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
