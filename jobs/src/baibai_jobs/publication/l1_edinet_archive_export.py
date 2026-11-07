"""保存EDINET pairから公開raw表を直接作る、通常経路へ未接続のadapter。"""

from __future__ import annotations

import hashlib
import shutil
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from itertools import groupby, zip_longest
from pathlib import Path

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from baibai_engine.data_collection.providers.edinet.archive_native import ArchiveMetadata
from baibai_engine.data_collection.providers.edinet.snapshot_contracts import (
    EDINET_SOURCE_ARCHIVES as DATASET,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.edinet_inputs import INPUT_PREFIX, read_edinet_input
from baibai_engine.storage.objects.r2 import ObjectStore
from baibai_engine.storage.snapshots.contracts import period_values
from baibai_engine.storage.snapshots.keys import dataset_manifest_key, validate_identifier
from baibai_engine.storage.snapshots.models import (
    DatasetManifest,
    LakeObject,
    ManifestTotals,
    NativeL1SourceRef,
    PartitionManifest,
    canonical_lake_model_bytes,
)
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_engine.storage.snapshots.writer import (
    LakeBuildError,
    LakeBuildReport,
    _build_period,
    _mirror_path,
    _promote_partitions,
    _workspace_path,
    _write_immutable,
)


def _validate_object(path: Path, obj: LakeObject, entries: tuple[ArchiveMetadata, ...]) -> None:
    """Verify decoded rows against fixed metadata, without rereading native bodies."""
    if sha256_file(path) != obj.sha256 or path.stat().st_size != obj.bytes:
        raise LakeBuildError("EDINET raw Parquet checksum or size differs")
    with pq.ParquetFile(path, pre_buffer=False) as parquet:
        if not parquet.schema_arrow.equals(
            DATASET.arrow_schema_for("l1_derived"), check_metadata=True
        ):
            raise LakeBuildError("EDINET raw Parquet schema differs")
        rows = (row for batch in parquet.iter_batches(batch_size=1) for row in batch.to_pylist())
        for entry, row in zip_longest(entries, rows):
            if (
                entry is None
                or row is None
                or row["doc_id"] != entry.doc_id
                or row["source_type"] != entry.source_type
                or row["stored_on"] != entry.stored_on.isoformat()
                or len(row["zip_content"]) != entry.size
                or hashlib.sha256(row["zip_content"]).hexdigest() != entry.sha256
            ):
                raise LakeBuildError("EDINET raw Parquet differs from fixed originals")
    keys = tuple((entry.doc_id, str(entry.source_type)) for entry in entries)
    if obj.rows != len(keys) or obj.min_key != min(keys) or obj.max_key != max(keys):
        raise LakeBuildError("EDINET raw Parquet inventory differs")


def export_edinet_archives_from_native_input(
    *,
    store: ObjectStore,
    saved: InputSnapshot,
    mirror_root: Path,
    producer_git_commit: str,
    build_id: str | None = None,
    created_at: datetime | None = None,
) -> LakeBuildReport:
    """Stream each selected original once; no SQLite, current head, or L2 fallback.

    This produces the new native provenance wire. Deploy its reader before enabling
    any normal publisher caller. It intentionally preserves opaque provider bytes,
    UTC stored_on, and partial coverage; it does not infer scope from received ZIPs.
    Every build rereads all selected bodies; content keys only avoid duplicate installs.
    """
    now = created_at or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("EDINET raw export clock requires a timezone")
    now = now.astimezone(UTC)
    fixed = read_edinet_input(store, saved)
    if now < fixed.selected_at:
        raise ValueError("EDINET raw export cannot predate its fixed selection")
    inventory = []
    for doc_id, source_type in fixed.archives.archive_keys:
        entry = fixed.archives.metadata(doc_id, source_type)
        assert entry is not None
        inventory.append(entry)
    entries = tuple(sorted(inventory, key=lambda item: (item.stored_on, *item.key)))
    if not entries:
        raise LakeBuildError("EDINET raw dataset holds no rows")
    assert fixed.identity.descriptor is not None
    digest = fixed.identity.descriptor["selection_sha256"]
    assert isinstance(digest, str)
    source = NativeL1SourceRef(
        kind="native_l1",
        source_id=f"native-l1-{digest[:24]}",
        sha256=digest,
        namespace="edinet",
        key=INPUT_PREFIX + "selections/" + digest + ".json",
    )
    actual_build_id = build_id or f"{now:%Y%m%dT%H%M%SZ}-edinet-{uuid.uuid4().hex[:8]}"
    validate_identifier(actual_build_id, label="build_id")
    staging = _workspace_path(mirror_root, "staging", actual_build_id)
    if staging.exists():
        raise LakeBuildError(f"build workspace already exists: {actual_build_id}")
    staging.mkdir(parents=True)

    def rows(group: tuple[ArchiveMetadata, ...]) -> Iterator[tuple[object, ...]]:
        for entry in group:
            yield (
                entry.doc_id,
                entry.source_type,
                entry.stored_on.isoformat(),
                fixed.archives.read_archive(entry.doc_id, entry.source_type),
            )

    try:
        built = []
        for day, items in groupby(entries, key=lambda entry: entry.stored_on):
            group = tuple(items)
            item = _build_period(
                dataset=DATASET,
                period=(day.year, day.month),
                staging_root=staging,
                rows=rows(group),
                sources=(source,),
                object_label=day.isoformat(),
            )
            _validate_object(item.staged_path, item.manifest.objects[0], group)
            built.append(item)
        partitions = tuple(
            PartitionManifest(
                values=period_values(DATASET, period),
                objects=tuple(obj for item in items for obj in item.manifest.objects),
                sources=(source,),
            )
            for period, items in groupby(built, key=lambda item: item.period)
        )
        manifest = DatasetManifest(
            manifest_version=2,
            dataset=DATASET.name,
            layer=DATASET.layer,
            contract_version=DATASET.contract_version,
            build_id=actual_build_id,
            sources=(),
            producer_git_commit=producer_git_commit,
            created_at=now,
            coverage_start=entries[0].stored_on,
            data_as_of=entries[-1].stored_on,
            population_count=None,
            coverage_status="partial",
            partition_by=DATASET.partition_by,
            partitions=partitions,
            totals=ManifestTotals(
                objects=len(built),
                bytes=sum(item.manifest.objects[0].bytes for item in built),
                rows=len(entries),
            ),
        )
        new_objects = _promote_partitions(mirror_root, built)
        path = _mirror_path(
            mirror_root, dataset_manifest_key(dataset=DATASET.name, build_id=actual_build_id)
        )
        _write_immutable(path, canonical_lake_model_bytes(manifest))
        return LakeBuildReport(path, manifest, len(partitions), new_objects)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
