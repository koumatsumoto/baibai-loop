"""Deterministic legacy SQLite to L1 Canonical Parquet export."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from baibai_engine.market.sqlite.schema import SQLITE_SCHEMA_VERSION
from baibai_engine.market.sqlite.snapshot import create_snapshot, validate_snapshot

from ..sqlite.coverage import daily_bars_covered_by_data, range_covered
from .datasets import PILOT_DATASETS, LakeDataset, require_pilot_dataset
from .immutable import ImmutableInstallError, install_immutable_bytes, install_immutable_file
from .keys import (
    canonical_object_key,
    dataset_manifest_key,
    sqlite_snapshot_object_key,
    validate_identifier,
)
from .models import (
    CoverageStatus,
    DatasetManifest,
    LakeObject,
    ManifestTotals,
    PartitionManifest,
    RawIngestSourceRef,
    SourceRef,
    SQLiteSnapshotSourceRef,
    canonical_lake_model_bytes,
    load_lake_model_json,
)
from .sources import resolve_source_ref, sha256_file

_ROW_GROUP_SIZE = 65_536
_PARQUET_VERSION = "2.6"
_COMPRESSION = "zstd"
_COMPRESSION_LEVEL = 9


class LakeBuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class LakeBuildReport:
    manifest_path: Path
    manifest: DatasetManifest
    changed_partitions: tuple[str, ...]
    reused_partitions: tuple[str, ...]
    created_objects: int


@dataclass(frozen=True)
class LakeBuildPlan:
    dataset: str
    affected_months: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class LakePilotBuildReport:
    snapshot: LegacySQLiteSnapshot
    datasets: Mapping[str, LakeBuildReport]


@dataclass(frozen=True)
class LegacySQLiteSnapshot:
    path: Path
    ref: SQLiteSnapshotSourceRef


@dataclass(frozen=True)
class _BuiltPartition:
    month: tuple[int, int]
    staged_path: Path
    manifest: PartitionManifest


def capture_legacy_sqlite_snapshot(
    *,
    sqlite_path: Path,
    mirror_root: Path,
    snapshot_id: str | None = None,
) -> LegacySQLiteSnapshot:
    """Capture committed main/WAL state once and install a content-addressed seed."""
    actual_id = snapshot_id or f"snapshot-{uuid.uuid4().hex}"
    validate_identifier(actual_id, label="snapshot_id")
    workspace = _workspace_path(mirror_root, "snapshot", actual_id)
    if workspace.exists():
        raise LakeBuildError(f"snapshot workspace already exists: {actual_id}")
    workspace.mkdir(parents=True)
    temporary = workspace / "snapshot.sqlite"
    try:
        required = max(sqlite_path.stat().st_size * 2, 64 * 1024 * 1024)
        if shutil.disk_usage(workspace).free < required:
            raise LakeBuildError("insufficient disk space for a sealed SQLite snapshot")
        create_snapshot(sqlite_path, temporary)
        schema_version = validate_snapshot(temporary)
        if schema_version != SQLITE_SCHEMA_VERSION:
            raise LakeBuildError(
                f"legacy SQLite schema is {schema_version}; expected {SQLITE_SCHEMA_VERSION}"
            )
        digest = sha256_file(temporary)
        key = sqlite_snapshot_object_key(
            snapshot_id=actual_id,
            schema_version=schema_version,
            content_sha256=digest,
        )
        target = _mirror_path(mirror_root, key)
        install_immutable_file(target, temporary, expected_sha256=digest)
        ref = SQLiteSnapshotSourceRef(
            kind="sqlite_snapshot",
            source_id=actual_id,
            key=key,
            sha256=digest,
            schema_version=schema_version,
        )
        resolve_source_ref(mirror_root, ref)
        return LegacySQLiteSnapshot(path=target, ref=ref)
    except (OSError, sqlite3.Error, ImmutableInstallError) as exc:
        raise LakeBuildError(f"SQLite snapshot capture failed: {exc}") from exc
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def export_legacy_sqlite(
    *,
    dataset_name: str,
    sqlite_path: Path,
    mirror_root: Path,
    producer_git_commit: str,
    start: date | None = None,
    end: date | None = None,
    base_manifest_path: Path | None = None,
    raw_source_refs: Sequence[RawIngestSourceRef] = (),
    source_snapshot_ref: SQLiteSnapshotSourceRef | None = None,
    build_id: str | None = None,
    created_at: datetime | None = None,
) -> LakeBuildReport:
    """Build whole affected months and reuse all unaffected base-manifest objects."""
    if (start is None) != (end is None):
        raise LakeBuildError("start and end must be supplied together")
    if start is not None and end is not None and start > end:
        raise LakeBuildError("start must not be after end")
    dataset = require_pilot_dataset(dataset_name)
    now = (created_at or datetime.now(UTC)).astimezone(UTC)
    transform = _transform_fingerprint(dataset)
    actual_build_id = build_id or (
        f"{now:%Y%m%dT%H%M%SZ}-legacy-{uuid.uuid4().hex[:8]}-{transform[-12:]}"
    )
    validate_identifier(actual_build_id, label="build_id")
    staging_root = _workspace_path(mirror_root, "staging", actual_build_id)
    quarantine_root = _workspace_path(mirror_root, "quarantine", actual_build_id)
    if staging_root.exists() or quarantine_root.exists():
        raise LakeBuildError(f"build workspace already exists: {actual_build_id}")
    snapshot = (
        capture_legacy_sqlite_snapshot(sqlite_path=sqlite_path, mirror_root=mirror_root)
        if source_snapshot_ref is None
        else LegacySQLiteSnapshot(
            path=resolve_source_ref(mirror_root, source_snapshot_ref),
            ref=source_snapshot_ref,
        )
    )
    for source in raw_source_refs:
        resolve_source_ref(mirror_root, source)
    current_sources: tuple[SourceRef, ...] = (snapshot.ref, *raw_source_refs)
    staging_root.mkdir(parents=True)

    try:
        with _open_immutable(snapshot.path) as connection:
            _validate_sqlite_contract(connection, dataset)
            base = _load_base_manifest(base_manifest_path, dataset, transform=transform)
            partitions = _base_partitions(base)
            months = _build_months(
                connection,
                dataset=dataset,
                base=base,
                start=start,
                end=end,
            )
            if not months and base is None:
                raise LakeBuildError("selected window contains no rows")
            built: list[_BuiltPartition] = []
            changed: list[str] = []
            reused: list[str] = []
            for month in months:
                previous = partitions.pop(month, None)
                rows = _month_rows(connection, dataset, month)
                if not rows:
                    if previous is not None:
                        changed.append(f"{month[0]:04d}-{month[1]:02d}")
                    continue
                item = _build_month(
                    connection,
                    dataset=dataset,
                    month=month,
                    staging_root=staging_root,
                    rows=rows,
                    sources=current_sources,
                )
                built.append(item)
                partitions[month] = item.manifest
                label = f"{month[0]:04d}-{month[1]:02d}"
                if previous == item.manifest:
                    reused.append(label)
                else:
                    changed.append(label)
            if not partitions:
                raise LakeBuildError("dataset manifest must contain at least one partition")
            data_as_of = _data_as_of(connection, dataset, months=partitions)
            coverage_status, coverage_start, population_count = _coverage_assessment(
                connection, dataset
            )

        # Every partition was checked against this sealed snapshot below. Refresh
        # reused lineage too, so a release names one coherent source generation.
        ordered = tuple(
            partitions[key].model_copy(
                update={
                    "sources": (
                        snapshot.ref,
                        *(
                            source
                            for source in partitions[key].sources
                            if source.kind != "sqlite_snapshot"
                        ),
                    )
                }
            )
            for key in sorted(partitions)
        )
        totals = ManifestTotals(
            objects=sum(len(item.objects) for item in ordered),
            bytes=sum(obj.bytes for item in ordered for obj in item.objects),
            rows=sum(obj.rows for item in ordered for obj in item.objects),
        )
        manifest = DatasetManifest(
            manifest_version=1,
            dataset=dataset.name,
            layer="l1_canonical",
            contract_version=dataset.contract_version,
            build_id=actual_build_id,
            sources=(),
            producer_git_commit=producer_git_commit,
            transform_fingerprint=transform,
            created_at=now,
            coverage_start=coverage_start,
            data_as_of=data_as_of,
            population_count=population_count,
            coverage_status=coverage_status,
            partition_by=dataset.partition_by,
            partitions=ordered,
            totals=totals,
        )
        created_objects = _promote_partitions(mirror_root, built)
        validate_legacy_parity(
            sqlite_path=snapshot.path,
            mirror_root=mirror_root,
            manifest=manifest,
        )
        manifest_path = _mirror_path(
            mirror_root,
            dataset_manifest_key(
                dataset=dataset.name,
                build_id=actual_build_id,
            ),
        )
        _write_immutable(manifest_path, canonical_lake_model_bytes(manifest))
        shutil.rmtree(staging_root)
        return LakeBuildReport(
            manifest_path=manifest_path,
            manifest=manifest,
            changed_partitions=tuple(changed),
            reused_partitions=tuple(reused),
            created_objects=created_objects,
        )
    except Exception as exc:
        if staging_root.exists():
            quarantine_root.parent.mkdir(parents=True, exist_ok=True)
            staging_root.replace(quarantine_root)
        if isinstance(exc, (LakeBuildError, ValueError)):
            raise
        raise LakeBuildError(str(exc)) from exc


def export_pilot_legacy(
    *,
    sqlite_path: Path,
    mirror_root: Path,
    producer_git_commit: str,
    base_manifest_paths: Mapping[str, Path] | None = None,
    created_at: datetime | None = None,
) -> LakePilotBuildReport:
    """Export both pilot datasets from one sealed SQLite generation."""
    bases = dict(base_manifest_paths or {})
    unknown = set(bases) - set(PILOT_DATASETS)
    if unknown:
        raise LakeBuildError(f"unsupported base manifest datasets: {sorted(unknown)}")
    snapshot = capture_legacy_sqlite_snapshot(
        sqlite_path=sqlite_path,
        mirror_root=mirror_root,
    )
    reports = {
        dataset_name: export_legacy_sqlite(
            dataset_name=dataset_name,
            sqlite_path=sqlite_path,
            mirror_root=mirror_root,
            producer_git_commit=producer_git_commit,
            base_manifest_path=bases.get(dataset_name),
            source_snapshot_ref=snapshot.ref,
            created_at=created_at,
        )
        for dataset_name in sorted(PILOT_DATASETS)
    }
    return LakePilotBuildReport(snapshot=snapshot, datasets=MappingProxyType(reports))


def validate_legacy_parity(
    *,
    sqlite_path: Path,
    mirror_root: Path,
    manifest: DatasetManifest,
    months: Iterable[tuple[int, int]] | None = None,
) -> None:
    """Require full exact parity; a requested range never weakens carried graph checks."""
    dataset = require_pilot_dataset(manifest.dataset)
    del months
    with _open_immutable(sqlite_path) as connection:
        _validate_sqlite_contract(connection, dataset)
        source_months = set(_selected_months(connection, dataset, start=None, end=None))
        manifest_months = {
            (int(item.values["year"]), int(item.values["month"])) for item in manifest.partitions
        }
        if source_months != manifest_months:
            raise LakeBuildError("SQLite and manifest month inventories differ")
        for partition in manifest.partitions:
            month = (int(partition.values["year"]), int(partition.values["month"]))
            rows = _month_rows(connection, dataset, month)
            if len(partition.objects) != 1:
                raise LakeBuildError("pilot partition must contain exactly one object")
            _validate_parquet(
                mirror_root / partition.objects[0].key,
                dataset=dataset,
                month=month,
                expected_rows=rows,
                expected_object=partition.objects[0],
            )
            current_state = _source_state_sha256(connection, dataset, month, rows)
            if current_state != partition.source_state_sha256:
                raise LakeBuildError(f"SQLite source state differs from manifest: {month}")


def plan_affected_months(
    *,
    dataset_name: str,
    sqlite_path: Path,
    base_manifest_path: Path,
) -> LakeBuildPlan:
    dataset = require_pilot_dataset(dataset_name)
    transform = _transform_fingerprint(dataset)
    base = _load_base_manifest(base_manifest_path, dataset, transform=transform)
    assert base is not None
    with _open_immutable(sqlite_path) as connection:
        _validate_sqlite_contract(connection, dataset)
        affected = _affected_months(connection, dataset, base)
    return LakeBuildPlan(dataset=dataset.name, affected_months=affected)


def _build_month(
    connection: sqlite3.Connection,
    *,
    dataset: LakeDataset,
    month: tuple[int, int],
    staging_root: Path,
    rows: Sequence[tuple[object, ...]],
    sources: tuple[SourceRef, ...],
) -> _BuiltPartition:
    table = pa.Table.from_pylist(
        [dict(zip((column.name for column in dataset.columns), row, strict=True)) for row in rows],
        schema=dataset.arrow_schema,
    )
    provisional = staging_root / f"{dataset.sqlite_table}-{month[0]:04d}-{month[1]:02d}.parquet"
    pq.write_table(
        table,
        provisional,
        compression=_COMPRESSION,
        compression_level=_COMPRESSION_LEVEL,
        data_page_version="1.0",
        row_group_size=_ROW_GROUP_SIZE,
        use_dictionary=False,
        version=_PARQUET_VERSION,
        write_statistics=True,
    )
    content_sha256 = _sha256(provisional)
    object_key = canonical_object_key(
        layer="l1_canonical",
        dataset=dataset.name,
        contract_version=dataset.contract_version,
        partition_values={"year": month[0], "month": month[1]},
        partition_by=dataset.partition_by,
        content_sha256=content_sha256,
    )
    staged = staging_root / object_key
    staged.parent.mkdir(parents=True, exist_ok=True)
    provisional.replace(staged)
    primary_keys = [tuple(str(row[index]) for index in _pk_indexes(dataset)) for row in rows]
    lake_object = LakeObject(
        key=object_key,
        sha256=content_sha256,
        bytes=staged.stat().st_size,
        rows=len(rows),
        min_key=min(primary_keys),
        max_key=max(primary_keys),
    )
    _validate_parquet(
        staged,
        dataset=dataset,
        month=month,
        expected_rows=rows,
        expected_object=lake_object,
    )
    return _BuiltPartition(
        month=month,
        staged_path=staged,
        manifest=PartitionManifest(
            values={"year": month[0], "month": month[1]},
            objects=(lake_object,),
            sources=sources,
            source_state_sha256=_source_state_sha256(connection, dataset, month, rows),
        ),
    )


def _build_months(
    connection: sqlite3.Connection,
    *,
    dataset: LakeDataset,
    base: DatasetManifest | None,
    start: date | None,
    end: date | None,
) -> tuple[tuple[int, int], ...]:
    if start is not None and end is not None:
        selected = set(_selected_months(connection, dataset, start=start, end=end))
        if base is not None:
            selected.update(
                (int(item.values["year"]), int(item.values["month"]))
                for item in base.partitions
                if (start.year, start.month)
                <= (int(item.values["year"]), int(item.values["month"]))
                <= (end.year, end.month)
            )
        return tuple(sorted(selected))
    if base is not None:
        return _affected_months(connection, dataset, base)
    return _selected_months(connection, dataset, start=None, end=None)


def _affected_months(
    connection: sqlite3.Connection,
    dataset: LakeDataset,
    base: DatasetManifest,
) -> tuple[tuple[int, int], ...]:
    current = set(_selected_months(connection, dataset, start=None, end=None))
    previous = _base_partitions(base)
    affected: list[tuple[int, int]] = []
    for month in sorted(current | set(previous)):
        partition = previous.get(month)
        rows = _month_rows(connection, dataset, month)
        if partition is None or not rows:
            affected.append(month)
            continue
        if partition.source_state_sha256 != _source_state_sha256(connection, dataset, month, rows):
            affected.append(month)
    return tuple(affected)


def _source_state_sha256(
    connection: sqlite3.Connection,
    dataset: LakeDataset,
    month: tuple[int, int],
    rows: Sequence[tuple[object, ...]],
) -> str:
    month_start = date(month[0], month[1], 1)
    month_end = date(month[0] + 1, 1, 1) if month[1] == 12 else date(month[0], month[1] + 1, 1)
    coverage = [
        tuple(row)
        for row in connection.execute(
            "SELECT MAX(coverage_start, ?), MIN(coverage_end, ?), status, error "
            "FROM source_coverage WHERE source = ? "
            "AND coverage_start IS NOT NULL AND coverage_end IS NOT NULL "
            "AND coverage_start < ? AND coverage_end >= ? ORDER BY 1, 2, 3, 4",
            (
                month_start.isoformat(),
                (month_end - date.resolution).isoformat(),
                dataset.sqlite_table,
                month_end.isoformat(),
                month_start.isoformat(),
            ),
        )
    ]
    payload = json.dumps(
        {"coverage": coverage, "rows": rows},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _coverage_assessment(
    connection: sqlite3.Connection, dataset: LakeDataset
) -> tuple[CoverageStatus, date, int]:
    """Derive completeness from the authority defined for the legacy source."""
    bounds = connection.execute(
        f"SELECT MIN({dataset.date_column}), MAX({dataset.date_column}) "  # nosec B608
        f"FROM {dataset.sqlite_table}"  # nosec B608
    ).fetchone()
    if bounds is None or bounds[0] is None or bounds[1] is None:
        raise LakeBuildError(f"{dataset.name} has no coverage bounds")
    start = date.fromisoformat(str(bounds[0]))
    end = date.fromisoformat(str(bounds[1]))
    population_count = int(
        connection.execute(
            f"SELECT COUNT(DISTINCT ticker) FROM {dataset.sqlite_table}"  # nosec B608
        ).fetchone()[0]
    )
    if population_count <= 0:
        raise LakeBuildError(f"{dataset.name} has no ticker population")
    if dataset.name == "jquants.daily_bars":
        status: CoverageStatus = (
            "complete" if daily_bars_covered_by_data(connection, start, end) else "partial"
        )
        return status, start, population_count
    if dataset.name == "jquants.short_sale_reports":
        has_non_ok = connection.execute(
            "SELECT 1 FROM source_coverage WHERE source = ? "
            "AND coverage_start <= ? AND coverage_end >= ? AND status != 'ok' LIMIT 1",
            (dataset.sqlite_table, end.isoformat(), start.isoformat()),
        ).fetchone()
        if has_non_ok is not None:
            return "partial", start, population_count
        status = (
            "complete" if range_covered(connection, dataset.sqlite_table, start, end) else "partial"
        )
        return status, start, population_count
    raise LakeBuildError(f"coverage authority is not defined for {dataset.name}")


def _validate_parquet(
    path: Path,
    *,
    dataset: LakeDataset,
    month: tuple[int, int],
    expected_rows: Sequence[tuple[object, ...]],
    expected_object: LakeObject,
) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise LakeBuildError(f"Parquet object missing or empty: {path}")
    if _sha256(path) != expected_object.sha256 or path.stat().st_size != expected_object.bytes:
        raise LakeBuildError(f"Parquet checksum or size mismatch: {path}")
    table = pq.read_table(path)
    if not table.schema.equals(dataset.arrow_schema, check_metadata=True):
        raise LakeBuildError(f"Parquet Arrow schema mismatch: {path}")
    names = tuple(column.name for column in dataset.columns)
    actual_rows = [tuple(item[name] for name in names) for item in table.to_pylist()]
    if actual_rows != list(expected_rows):
        raise LakeBuildError(f"Parquet values or row order differ from SQLite: {path}")
    pk_indexes = _pk_indexes(dataset)
    keys = [tuple(str(row[index]) for index in pk_indexes) for row in actual_rows]
    if len(keys) != len(set(keys)):
        raise LakeBuildError(f"Parquet primary key is not unique: {path}")
    date_index = names.index(dataset.date_column)
    prefix = f"{month[0]:04d}-{month[1]:02d}-"
    if any(not str(row[date_index]).startswith(prefix) for row in actual_rows):
        raise LakeBuildError(f"Parquet row escapes its year/month partition: {path}")
    if len(actual_rows) != expected_object.rows:
        raise LakeBuildError(f"Parquet row count mismatch: {path}")
    if min(keys) != expected_object.min_key or max(keys) != expected_object.max_key:
        raise LakeBuildError(f"Parquet min/max primary key mismatch: {path}")


def _selected_months(
    connection: sqlite3.Connection,
    dataset: LakeDataset,
    *,
    start: date | None,
    end: date | None,
) -> tuple[tuple[int, int], ...]:
    where = ""
    params: tuple[str, ...] = ()
    if start is not None and end is not None:
        where = f"WHERE {dataset.date_column} BETWEEN ? AND ?"  # nosec B608
        params = (start.isoformat(), end.isoformat())
    query = (
        f"SELECT DISTINCT substr({dataset.date_column}, 1, 4), "  # nosec B608
        f"substr({dataset.date_column}, 6, 2) FROM {dataset.sqlite_table} "
        f"{where} ORDER BY 1, 2"
    )
    return tuple((int(year), int(month)) for year, month in connection.execute(query, params))


def _month_rows(
    connection: sqlite3.Connection,
    dataset: LakeDataset,
    month: tuple[int, int],
) -> list[tuple[object, ...]]:
    prefix = f"{month[0]:04d}-{month[1]:02d}-"
    columns = ", ".join(column.name for column in dataset.columns)
    order = ", ".join(dataset.primary_key)
    query = (
        f"SELECT {columns} FROM {dataset.sqlite_table} "  # nosec B608
        f"WHERE {dataset.date_column} LIKE ? ORDER BY {order}"
    )
    return [tuple(row) for row in connection.execute(query, (f"{prefix}%",))]


def _validate_sqlite_contract(connection: sqlite3.Connection, dataset: LakeDataset) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != SQLITE_SCHEMA_VERSION:
        raise LakeBuildError(f"legacy SQLite schema is {version}; expected {SQLITE_SCHEMA_VERSION}")
    actual = tuple(connection.execute(f"PRAGMA table_info({dataset.sqlite_table})"))
    expected = tuple(
        (
            index,
            column.name,
            column.sqlite_type,
            0 if column.nullable else 1,
            None,
            column.primary_key_ordinal,
        )
        for index, column in enumerate(dataset.columns)
    )
    if actual != expected:
        raise LakeBuildError(f"legacy SQLite table contract mismatch: {dataset.sqlite_table}")


def _load_base_manifest(
    path: Path | None,
    dataset: LakeDataset,
    *,
    transform: str,
) -> DatasetManifest | None:
    if path is None:
        return None
    try:
        value = load_lake_model_json(path.read_bytes(), DatasetManifest)
    except Exception as exc:
        raise LakeBuildError(f"invalid base dataset manifest: {path}: {exc}") from exc
    if (
        value.dataset != dataset.name
        or value.layer != "l1_canonical"
        or value.contract_version != dataset.contract_version
        or tuple(value.partition_by) != dataset.partition_by
    ):
        raise LakeBuildError("base manifest does not match the requested dataset contract")
    if value.transform_fingerprint != transform:
        raise LakeBuildError(
            "base manifest transform_fingerprint differs; run a full rebuild without "
            "--base-manifest"
        )
    return value


def _base_partitions(
    manifest: DatasetManifest | None,
) -> dict[tuple[int, int], PartitionManifest]:
    if manifest is None:
        return {}
    return {
        (int(item.values["year"]), int(item.values["month"])): item for item in manifest.partitions
    }


def _promote_partitions(mirror_root: Path, built: Sequence[_BuiltPartition]) -> int:
    created = 0
    for item in built:
        target = _mirror_path(mirror_root, item.manifest.objects[0].key)
        try:
            if install_immutable_file(
                target,
                item.staged_path,
                expected_sha256=item.manifest.objects[0].sha256,
            ):
                created += 1
        except ImmutableInstallError as exc:
            raise LakeBuildError(f"content-addressed key collision: {target}") from exc
    return created


def _data_as_of(
    connection: sqlite3.Connection,
    dataset: LakeDataset,
    *,
    months: Iterable[tuple[int, int]],
) -> date:
    latest_month = max(months)
    names = tuple(column.name for column in dataset.columns)
    date_index = names.index(dataset.date_column)
    rows = _month_rows(connection, dataset, latest_month)
    if not rows:
        raise LakeBuildError("latest manifest partition is absent from the legacy SQLite")
    return max(date.fromisoformat(str(row[date_index])) for row in rows)


def _open_immutable(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)


def _pk_indexes(dataset: LakeDataset) -> tuple[int, ...]:
    names = tuple(column.name for column in dataset.columns)
    return tuple(names.index(name) for name in dataset.primary_key)


def _transform_fingerprint(dataset: LakeDataset) -> str:
    contract = {
        "arrow_schema": str(dataset.arrow_schema),
        "compression": _COMPRESSION,
        "compression_level": _COMPRESSION_LEVEL,
        "contract_version": dataset.contract_version,
        "dataset": dataset.name,
        "parquet_version": _PARQUET_VERSION,
        "partition_by": dataset.partition_by,
        "row_group_size": _ROW_GROUP_SIZE,
        "source_kind": "legacy_sqlite_import",
        "writer": f"pyarrow-{pa.__version__}",
        "implementation_sha256": {
            name: sha256_file(Path(__file__).with_name(name))
            for name in ("datasets.py", "writer.py")
        },
    }
    payload = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_immutable(path: Path, payload: bytes) -> None:
    try:
        install_immutable_bytes(
            path,
            payload,
            validate=lambda value: load_lake_model_json(value, DatasetManifest),
        )
    except ImmutableInstallError as exc:
        raise LakeBuildError(str(exc)) from exc


def _workspace_path(mirror_root: Path, area: str, identifier: str) -> Path:
    root = mirror_root.resolve()
    parent = root / "lake" / area
    if parent.exists() and parent.is_symlink():
        raise LakeBuildError(f"lake {area} workspace cannot be a symlink")
    resolved_parent = parent.resolve()
    if not resolved_parent.is_relative_to(root):
        raise LakeBuildError(f"lake {area} workspace escapes the mirror")
    path = (resolved_parent / identifier).resolve()
    if not path.is_relative_to(resolved_parent):
        raise LakeBuildError(f"lake {area} workspace escapes the mirror")
    return path


def _mirror_path(mirror_root: Path, key: str) -> Path:
    root = mirror_root.resolve()
    path = (root / key).resolve()
    if not path.is_relative_to(root):
        raise LakeBuildError("lake object escapes mirror root")
    return path
