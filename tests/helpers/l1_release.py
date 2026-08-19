"""Build a minimal but real L1 release inside a mirror, closure and all.

Resolving an `L1ReleaseSourceRef` walks what it roots — dataset manifests and every
Parquet object they name — so a stand-in payload no longer stands in. Tests that need
a resolvable release build one here rather than each inventing a shape production
never produces.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

from baibai_engine.market.lake.datasets import LAKE_DATASETS
from baibai_engine.market.lake.keys import dataset_manifest_key, release_manifest_key
from baibai_engine.market.lake.models import (
    DatasetManifest,
    L1ReleaseSourceRef,
    LakeObject,
    ManifestTotals,
    PartitionManifest,
    ReleaseDataset,
    ReleaseManifest,
    canonical_lake_model_bytes,
)
from tests.helpers.calibration_store import synthetic_calibration_source


def release_source(release_id: str = "20260130T000000Z-release") -> L1ReleaseSourceRef:
    return L1ReleaseSourceRef(
        kind="l1_release",
        source_id=release_id,
        key=release_manifest_key(release_id=release_id),
        sha256="b" * 64,
        manifest_version=1,
    )


def stored_release_source(root: Path) -> tuple[Path, L1ReleaseSourceRef]:
    """A retained source whose whole closure is on disk, and the manifest file it names.

    A stand-in payload was enough while resolving one meant hashing one file. Resolving
    an L1 release now walks what it roots — dataset manifests and every Parquet object
    they name — so the fixture has to be a release the mirror actually holds, or the
    test would be asserting against a shape production never sees.
    """

    reference = release_source()
    dataset = LAKE_DATASETS["jquants.daily_bars"]
    body = b"parquet-bytes-for-test"
    object_key = (
        f"lake/l1/canonical/{dataset.name}/contract=v{dataset.contract_version}"
        f"/year=2026/month=1/part-{hashlib.sha256(body).hexdigest()}.parquet"
    )
    object_path = root / object_key
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(body)
    lake_object = LakeObject(
        key=object_key,
        sha256=hashlib.sha256(body).hexdigest(),
        bytes=len(body),
        rows=1,
        min_key=("1301", "2026-01-05"),
        max_key=("1301", "2026-01-05"),
    )
    partition = PartitionManifest(
        values={"year": 2026, "month": 1},
        objects=(lake_object,),
        sources=(synthetic_calibration_source(captured_on=date(2026, 1, 31)),),
        source_state_sha256="d" * 64,
    )
    dataset_manifest = DatasetManifest(
        manifest_version=1,
        dataset=dataset.name,
        layer="l1_canonical",
        contract_version=dataset.contract_version,
        build_id="20260130T000000Z-legacy-abcdef01-0123456789ab",
        created_at=datetime(2026, 1, 30, tzinfo=UTC),
        producer_git_commit="1" * 40,
        transform_fingerprint=f"sha256:{'e' * 64}",
        partition_by=dataset.partition_by,
        partitions=(partition,),
        totals=ManifestTotals(objects=1, bytes=len(body), rows=1),
        coverage_status="partial",
        coverage_start=date(2026, 1, 1),
        data_as_of=date(2026, 1, 31),
        population_count=1,
        sources=(),
    )
    dataset_bytes = canonical_lake_model_bytes(dataset_manifest)
    dataset_path = root / dataset_manifest_key(
        dataset=dataset.name, build_id=dataset_manifest.build_id
    )
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    dataset_path.write_bytes(dataset_bytes)
    release = ReleaseManifest(
        manifest_version=1,
        release_id=reference.source_id,
        profile="production",
        created_at=datetime(2026, 1, 30, tzinfo=UTC),
        data_as_of=date(2026, 1, 31),
        datasets={
            dataset.name: ReleaseDataset(
                build_id=dataset_manifest.build_id,
                contract_version=dataset.contract_version,
                manifest_sha256=hashlib.sha256(dataset_bytes).hexdigest(),
                data_as_of=dataset_manifest.data_as_of,
                coverage_status=dataset_manifest.coverage_status,
                totals=dataset_manifest.totals,
            )
        },
    )
    payload = canonical_lake_model_bytes(release)
    stored = root / reference.key
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(payload)
    return stored, reference.model_copy(update={"sha256": hashlib.sha256(payload).hexdigest()})
