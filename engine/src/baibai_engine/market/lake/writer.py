"""Deterministic legacy SQLite to L1 Canonical Parquet export."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from baibai_engine.foundation.source_identity import release_line, semantic_source_digest
from baibai_engine.market.sqlite.lake_origin import (
    LakeStoreOrigin,
    LakeStoreOriginError,
    read_lake_store_origin_from_connection,
)
from baibai_engine.market.sqlite.schema import SQLITE_SCHEMA_VERSION
from baibai_engine.market.sqlite.snapshot import create_snapshot, validate_snapshot

from ..sqlite.coverage import daily_bars_covered_by_data, range_covered
from .datasets import (
    LAKE_DATASETS,
    LakeDataset,
    Period,
    period_bounds,
    period_label,
    period_values,
    require_lake_dataset,
)
from .immutable import ImmutableInstallError, install_immutable_bytes, install_immutable_file
from .keys import canonical_object_key, dataset_manifest_key, validate_identifier
from .models import (
    CoverageStatus,
    DatasetManifest,
    LakeObject,
    ManifestTotals,
    PartitionManifest,
    SourceRef,
    SQLiteSnapshotSourceRef,
    canonical_lake_model_bytes,
    load_lake_model_json,
)
from .sources import sha256_file, validate_sqlite_snapshot

_ROW_GROUP_SIZE = 65_536
_PARQUET_VERSION = "2.6"
_COMPRESSION = "zstd"
_COMPRESSION_LEVEL = 9


class LakeBuildError(RuntimeError):
    pass


class LakeTransformFingerprintMismatch(LakeBuildError):
    """A differential build cannot reuse a base made by another transform."""


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
    affected_periods: tuple[Period, ...]


@dataclass(frozen=True)
class LakeExportReport:
    snapshot: SQLiteSnapshotSourceRef
    store_origin: LakeStoreOrigin | None
    datasets: Mapping[str, LakeBuildReport]
    empty_datasets: tuple[str, ...] = ()
    """Datasets the registry declares whose source has not published a row yet."""


@dataclass(frozen=True)
class LegacySQLiteSnapshot:
    """A sealed read of the legacy store, valid only inside the operation that took it."""

    path: Path
    ref: SQLiteSnapshotSourceRef


@dataclass(frozen=True)
class _BuiltPartition:
    period: Period
    staged_path: Path
    manifest: PartitionManifest


@contextmanager
def sealed_sqlite_snapshot(
    *,
    sqlite_path: Path,
    mirror_root: Path,
    snapshot_id: str | None = None,
) -> Iterator[LegacySQLiteSnapshot]:
    """Give one operation a fixed read of the legacy store, then take it back.

    The sealed copy is the size of the whole store. It exists to stop the store from
    moving underneath a build that reads it for minutes, which is a property of the
    operation and not of anything the operation publishes — so it is scoped to the
    operation. What outlives it is the identity in the reference, which is what lets a
    later reader ask whether a store generation it holds is the one a cohort read. The
    seal is byte-deterministic for an unchanged store, so that question is answerable by
    re-sealing the store and comparing digests.
    """

    captured_at = datetime.now(UTC)
    actual_id = snapshot_id or f"snapshot-{uuid.uuid4().hex}"
    validate_identifier(actual_id, label="snapshot_id")
    workspace = _workspace_path(mirror_root, "staging", actual_id)
    if workspace.exists():
        raise LakeBuildError(f"snapshot workspace already exists: {actual_id}")
    workspace.mkdir(parents=True)
    sealed = workspace / "snapshot.sqlite"
    try:
        required = max(sqlite_path.stat().st_size * 2, 64 * 1024 * 1024)
        if shutil.disk_usage(workspace).free < required:
            raise LakeBuildError("insufficient disk space for a sealed SQLite snapshot")
        create_snapshot(sqlite_path, sealed)
        schema_version = validate_snapshot(sealed)
        if schema_version != SQLITE_SCHEMA_VERSION:
            raise LakeBuildError(
                f"legacy SQLite schema is {schema_version}; expected {SQLITE_SCHEMA_VERSION}"
            )
        digest = sha256_file(sealed)
        validate_sqlite_snapshot(sealed, expected_schema_version=schema_version)
        ref = SQLiteSnapshotSourceRef(
            kind="sqlite_snapshot",
            source_id=f"market-v{schema_version}-{digest[:24]}",
            sha256=digest,
            schema_version=schema_version,
            captured_at=captured_at,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        shutil.rmtree(workspace, ignore_errors=True)
        raise LakeBuildError(f"SQLite snapshot capture failed: {exc}") from exc
    try:
        yield LegacySQLiteSnapshot(path=sealed, ref=ref)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def export_legacy_sqlite(
    *,
    dataset_name: str,
    mirror_root: Path,
    producer_git_commit: str,
    start: date | None = None,
    end: date | None = None,
    base_manifest_path: Path | None = None,
    source_snapshot: LegacySQLiteSnapshot,
    build_id: str | None = None,
    created_at: datetime | None = None,
    audit_full_history: bool = False,
) -> LakeBuildReport:
    """Build whole affected months and reuse all unaffected base-manifest objects.

    The sealed snapshot is passed in rather than captured here: it belongs to the
    operation, and one export writes every dataset from one seal.

    Parity is checked on the months this build wrote. A carried object is addressed by
    the digest of its own bytes, so "unchanged" is an identity rather than a claim to
    re-prove: different bytes would be a different key and the manifest reference would
    stop resolving. Re-deriving every carried month from SQLite on every run re-proves
    the previous build instead of this one, and makes a one month correction cost the
    whole history. ``audit_full_history`` asks for that proof explicitly, for the
    occasions where the question is whether the store as a whole still agrees with
    SQLite rather than whether this build is correct.
    """

    if (start is None) != (end is None):
        raise LakeBuildError("start and end must be supplied together")
    if start is not None and end is not None and start > end:
        raise LakeBuildError("start must not be after end")
    dataset = require_lake_dataset(dataset_name)
    now = (created_at or datetime.now(UTC)).astimezone(UTC)
    transform = _transform_fingerprint(dataset)
    actual_build_id = build_id or (
        f"{now:%Y%m%dT%H%M%SZ}-legacy-{uuid.uuid4().hex[:8]}-{transform[-12:]}"
    )
    validate_identifier(actual_build_id, label="build_id")
    staging_root = _workspace_path(mirror_root, "staging", actual_build_id)
    if staging_root.exists():
        raise LakeBuildError(f"build workspace already exists: {actual_build_id}")
    snapshot = source_snapshot
    staging_root.mkdir(parents=True)

    try:
        with _open_immutable(snapshot.path) as connection:
            _validate_sqlite_contract(connection, dataset)
            base = _load_base_manifest(base_manifest_path, dataset, transform=transform)
            partitions = _base_partitions(base)
            periods = _build_periods(
                connection,
                dataset=dataset,
                base=base,
                start=start,
                end=end,
            )
            if not periods and base is None:
                raise LakeBuildError("selected window contains no rows")
            built: list[_BuiltPartition] = []
            changed: list[str] = []
            reused: list[str] = []
            for period in periods:
                previous = partitions.pop(period, None)
                rows = _period_rows(connection, dataset, period)
                label = period_label(period)
                if not rows:
                    if previous is not None:
                        changed.append(label)
                    continue
                item = _build_period(
                    connection,
                    dataset=dataset,
                    period=period,
                    staging_root=staging_root,
                    rows=rows,
                    sources=(snapshot.ref,),
                )
                built.append(item)
                partitions[period] = item.manifest
                if previous == item.manifest:
                    reused.append(label)
                else:
                    changed.append(label)
            if not partitions:
                raise LakeBuildError("dataset manifest must contain at least one partition")
            data_as_of = _data_as_of(connection, dataset, periods=partitions)
            coverage_status, coverage_start, population_count = _coverage_assessment(
                connection, dataset
            )

        # Every partition was checked against this sealed snapshot below. Refresh
        # reused lineage too, so a release names one coherent source generation.
        ordered = tuple(
            partitions[key].model_copy(update={"sources": (snapshot.ref,)})
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
            periods=(None if audit_full_history else tuple(item.period for item in built)),
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
        # The staging tree is left where it is. It is under no manifest, so the collector
        # takes it on the staging grace, and moving it somewhere with a longer one would
        # only keep an accident report nobody opens.
        if isinstance(exc, (LakeBuildError, ValueError)):
            raise
        raise LakeBuildError(str(exc)) from exc


def export_lake_legacy(
    *,
    sqlite_path: Path,
    mirror_root: Path,
    producer_git_commit: str,
    expected_store_origin: LakeStoreOrigin | None,
    base_manifest_paths: Mapping[str, Path] | None = None,
    created_at: datetime | None = None,
    audit_full_history: bool = False,
) -> LakeExportReport:
    """Export every lake dataset from one sealed SQLite generation."""
    bases = dict(base_manifest_paths or {})
    unknown = set(bases) - set(LAKE_DATASETS)
    if unknown:
        raise LakeBuildError(f"unsupported base manifest datasets: {sorted(unknown)}")
    with sealed_sqlite_snapshot(sqlite_path=sqlite_path, mirror_root=mirror_root) as snapshot:
        with _open_immutable(snapshot.path) as probe:
            try:
                store_origin = read_lake_store_origin_from_connection(probe)
            except LakeStoreOriginError as exc:
                raise LakeBuildError(f"sealed SQLite store origin is invalid: {exc}") from exc
            if store_origin != expected_store_origin:
                raise LakeBuildError(
                    "sealed SQLite store origin differs from the serving L1 release; "
                    "hydrate from current before publishing"
                )
            # A dataset whose source has not started publishing yet holds no row, and a
            # canonical build with no partition is not a release input. Skipping it keeps
            # "this source has not begun" distinct from "this build failed", which is the
            # difference an operator needs on the day the source does begin.
            populated = tuple(
                name for name in sorted(LAKE_DATASETS) if _has_rows(probe, LAKE_DATASETS[name])
            )
        if not populated:
            raise LakeBuildError("no lake dataset holds a row in this SQLite generation")
        reports = {
            dataset_name: export_legacy_sqlite(
                dataset_name=dataset_name,
                mirror_root=mirror_root,
                producer_git_commit=producer_git_commit,
                base_manifest_path=bases.get(dataset_name),
                source_snapshot=snapshot,
                created_at=created_at,
                audit_full_history=audit_full_history,
            )
            for dataset_name in populated
        }
        return LakeExportReport(
            snapshot=snapshot.ref,
            store_origin=store_origin,
            datasets=MappingProxyType(reports),
            empty_datasets=tuple(name for name in sorted(LAKE_DATASETS) if name not in reports),
        )


def _has_rows(connection: sqlite3.Connection, dataset: LakeDataset) -> bool:
    row = connection.execute(
        f"SELECT 1 FROM {dataset.sqlite_table} LIMIT 1"  # nosec B608
    ).fetchone()
    return row is not None


def validate_legacy_parity(
    *,
    sqlite_path: Path,
    mirror_root: Path,
    manifest: DatasetManifest,
    periods: Iterable[Period] | None = None,
) -> None:
    """Check that the manifest describes the same periods SQLite holds, and their rows.

    The period inventory is always compared in full: a period present in one side and
    absent from the other is a hole no per-partition check would look at, and answering
    it costs one query.

    ``periods`` restricts the row-level comparison to the partitions a caller actually
    wrote. Passing ``None`` compares every partition, which is what a first export does
    by construction and what an explicit audit asks for.
    """
    dataset = require_lake_dataset(manifest.dataset)
    selected = None if periods is None else set(periods)
    with _open_immutable(sqlite_path) as connection:
        _validate_sqlite_contract(connection, dataset)
        source_periods = set(_selected_periods(connection, dataset, start=None, end=None))
        manifest_periods = set(_base_partitions(manifest))
        if source_periods != manifest_periods:
            raise LakeBuildError("SQLite and manifest partition inventories differ")
        for period, partition in _base_partitions(manifest).items():
            if selected is not None and period not in selected:
                continue
            rows = _period_rows(connection, dataset, period)
            if len(partition.objects) != 1:
                raise LakeBuildError("a canonical partition must contain exactly one object")
            _validate_parquet(
                mirror_root / partition.objects[0].key,
                dataset=dataset,
                period=period,
                expected_rows=rows,
                expected_object=partition.objects[0],
            )
            current_state = _source_state_sha256(connection, dataset, period, rows)
            if current_state != partition.source_state_sha256:
                raise LakeBuildError(
                    f"SQLite source state differs from manifest: {period_label(period)}"
                )


def plan_affected_periods(
    *,
    dataset_name: str,
    sqlite_path: Path,
    base_manifest_path: Path,
) -> LakeBuildPlan:
    dataset = require_lake_dataset(dataset_name)
    transform = _transform_fingerprint(dataset)
    base = _load_base_manifest(base_manifest_path, dataset, transform=transform)
    assert base is not None
    with _open_immutable(sqlite_path) as connection:
        _validate_sqlite_contract(connection, dataset)
        affected = affected_periods(connection, dataset, base)
    return LakeBuildPlan(dataset=dataset.name, affected_periods=affected)


def _build_period(
    connection: sqlite3.Connection,
    *,
    dataset: LakeDataset,
    period: Period,
    staging_root: Path,
    rows: Sequence[tuple[object, ...]],
    sources: tuple[SourceRef, ...],
) -> _BuiltPartition:
    table = pa.Table.from_pylist(
        [dict(zip((column.name for column in dataset.columns), row, strict=True)) for row in rows],
        schema=dataset.arrow_schema,
    )
    provisional = staging_root / f"{dataset.sqlite_table}-{period_label(period)}.parquet"
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
        partition_values=period_values(dataset, period),
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
        period=period,
        expected_rows=rows,
        expected_object=lake_object,
    )
    return _BuiltPartition(
        period=period,
        staged_path=staged,
        manifest=PartitionManifest(
            values=period_values(dataset, period),
            objects=(lake_object,),
            sources=sources,
            source_state_sha256=_source_state_sha256(connection, dataset, period, rows),
        ),
    )


def _build_periods(
    connection: sqlite3.Connection,
    *,
    dataset: LakeDataset,
    base: DatasetManifest | None,
    start: date | None,
    end: date | None,
) -> tuple[Period, ...]:
    if start is not None and end is not None:
        selected = set(_selected_periods(connection, dataset, start=start, end=end))
        if base is not None:
            # A base partition that the window touches is rebuilt even when SQLite now
            # holds no row in it, which is how a deletion reaches the manifest. The
            # bounds are the window's own periods, so the comparison stays inside one
            # grain rather than mixing a month against a year.
            first = _period_of(dataset, start)
            last = _period_of(dataset, end)
            selected.update(period for period in _base_partitions(base) if first <= period <= last)
        return tuple(sorted(selected))
    if base is not None:
        return affected_periods(connection, dataset, base)
    return _selected_periods(connection, dataset, start=None, end=None)


def affected_periods(
    connection: sqlite3.Connection,
    dataset: LakeDataset,
    base: DatasetManifest,
) -> tuple[Period, ...]:
    """Return every partition whose rows or relevant coverage differ from ``base``.

    Publication uses this to decide what to rebuild. Read-only provenance checks reuse
    the same comparison so they cannot call a store rebuildable from a release under a
    weaker definition of equality than the publisher itself.
    """

    current = set(_selected_periods(connection, dataset, start=None, end=None))
    previous = _base_partitions(base)
    affected: list[Period] = []
    for period in sorted(current | set(previous)):
        partition = previous.get(period)
        rows = _period_rows(connection, dataset, period)
        if partition is None or not rows:
            affected.append(period)
            continue
        if partition.source_state_sha256 != _source_state_sha256(connection, dataset, period, rows):
            affected.append(period)
    return tuple(affected)


def _source_state_sha256(
    connection: sqlite3.Connection,
    dataset: LakeDataset,
    period: Period,
    rows: Sequence[tuple[object, ...]],
) -> str:
    period_start, period_end = period_bounds(period)
    coverage = [
        tuple(row)
        for row in connection.execute(
            "SELECT MAX(coverage_start, ?), MIN(coverage_end, ?), status, error "
            "FROM source_coverage WHERE source = ? "
            "AND coverage_start IS NOT NULL AND coverage_end IS NOT NULL "
            "AND coverage_start < ? AND coverage_end >= ? ORDER BY 1, 2, 3, 4",
            (
                period_start.isoformat(),
                (period_end - date.resolution).isoformat(),
                dataset.coverage_source_name,
                period_end.isoformat(),
                period_start.isoformat(),
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
) -> tuple[CoverageStatus, date, int | None]:
    """Derive completeness from the authority defined for the legacy source."""
    bounds = connection.execute(
        f"SELECT MIN({dataset.date_column}), MAX({dataset.date_column}) "  # nosec B608
        f"FROM {dataset.sqlite_table}"  # nosec B608
    ).fetchone()
    if bounds is None or bounds[0] is None or bounds[1] is None:
        raise LakeBuildError(f"{dataset.name} has no coverage bounds")
    start = _iso_date(bounds[0], dataset=dataset)
    end = _iso_date(bounds[1], dataset=dataset)
    population_count: int | None = None
    if dataset.population_column is not None:
        population_count = int(
            connection.execute(
                f"SELECT COUNT(DISTINCT {dataset.population_column}) "  # nosec B608
                f"FROM {dataset.sqlite_table}"  # nosec B608
            ).fetchone()[0]
        )
        if population_count <= 0:
            raise LakeBuildError(f"{dataset.name} has no {dataset.population_column} population")
    return _coverage_status(connection, dataset, start=start, end=end), start, population_count


def _coverage_status(
    connection: sqlite3.Connection, dataset: LakeDataset, *, start: date, end: date
) -> CoverageStatus:
    if dataset.coverage_authority == "daily_bars_rows":
        return "complete" if daily_bars_covered_by_data(connection, start, end) else "partial"
    if dataset.coverage_authority == "unproven":
        # Nothing records what was fetched and the rows carry no invariant to check them
        # against, so the build can say what it holds and not that it holds everything.
        return "partial"
    source = dataset.coverage_source_name
    has_non_ok = connection.execute(
        "SELECT 1 FROM source_coverage WHERE source = ? "
        "AND coverage_start <= ? AND coverage_end >= ? AND status != 'ok' LIMIT 1",
        (source, end.isoformat(), start.isoformat()),
    ).fetchone()
    if has_non_ok is not None:
        return "partial"
    return "complete" if range_covered(connection, source, start, end) else "partial"


def _validate_parquet(
    path: Path,
    *,
    dataset: LakeDataset,
    period: Period,
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
    prefix = f"{period_label(period)}-"
    if any(not str(row[date_index]).startswith(prefix) for row in actual_rows):
        raise LakeBuildError(f"Parquet row escapes its calendar partition: {path}")
    if len(actual_rows) != expected_object.rows:
        raise LakeBuildError(f"Parquet row count mismatch: {path}")
    if min(keys) != expected_object.min_key or max(keys) != expected_object.max_key:
        raise LakeBuildError(f"Parquet min/max primary key mismatch: {path}")


def _selected_periods(
    connection: sqlite3.Connection,
    dataset: LakeDataset,
    *,
    start: date | None,
    end: date | None,
) -> tuple[Period, ...]:
    where = ""
    params: tuple[str, ...] = ()
    if start is not None and end is not None:
        where = f"WHERE {dataset.date_column} BETWEEN ? AND ?"  # nosec B608
        params = (start.isoformat(), end.isoformat())
    parts = _period_sql_parts(dataset)
    query = (
        f"SELECT DISTINCT {', '.join(parts)} FROM {dataset.sqlite_table} "  # nosec B608
        f"{where} ORDER BY {', '.join(str(index) for index in range(1, len(parts) + 1))}"
    )
    return tuple(tuple(int(part) for part in row) for row in connection.execute(query, params))


def _period_sql_parts(dataset: LakeDataset) -> tuple[str, ...]:
    """The ISO date substrings that name one partition, in layout order.

    ISO dates sort and slice as text, so the grain is a prefix length rather than a date
    function: the same expression names the partition and orders the result.
    """

    year = f"substr({dataset.date_column}, 1, 4)"
    if dataset.partition_grain == "year":
        return (year,)
    return (year, f"substr({dataset.date_column}, 6, 2)")


def _period_of(dataset: LakeDataset, day: date) -> Period:
    if dataset.partition_grain == "year":
        return (day.year,)
    return (day.year, day.month)


def _period_rows(
    connection: sqlite3.Connection,
    dataset: LakeDataset,
    period: Period,
) -> list[tuple[object, ...]]:
    prefix = f"{period_label(period)}-"
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
        raise LakeTransformFingerprintMismatch(
            "base manifest transform_fingerprint differs; run a full rebuild without "
            "--base-manifest"
        )
    return value


def _base_partitions(
    manifest: DatasetManifest | None,
) -> dict[Period, PartitionManifest]:
    """Key each partition by the calendar period its own manifest layout names.

    The layout is read from the manifest rather than the dataset contract so that a
    manifest written under a different grain is keyed by what it actually says. The
    reader refuses that manifest separately; keying it by today's contract instead would
    silently reinterpret its partitions first.
    """

    if manifest is None:
        return {}
    layout = tuple(manifest.partition_by)
    return {tuple(int(item.values[name]) for name in layout): item for item in manifest.partitions}


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
    periods: Iterable[Period],
) -> date:
    latest = max(periods)
    names = tuple(column.name for column in dataset.columns)
    date_index = names.index(dataset.date_column)
    rows = _period_rows(connection, dataset, latest)
    if not rows:
        raise LakeBuildError("latest manifest partition is absent from the legacy SQLite")
    return max(_iso_date(row[date_index], dataset=dataset) for row in rows)


def _iso_date(value: object, *, dataset: LakeDataset) -> date:
    if not isinstance(value, str):
        raise LakeBuildError(f"{dataset.name} date column must contain ISO text")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise LakeBuildError(f"{dataset.name} date column must contain ISO text") from None


def _open_immutable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA case_sensitive_like=ON")
    return connection


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
        # Only the release line: a patch release does not change the file format, and
        # 25.0.0 to 25.0.1 was measured to write byte-identical Parquet while stopping the
        # daily batch. A minor or major bump still forces the rebuild.
        "writer": f"pyarrow-{release_line(pa.__version__)}",
        # Coverage semantics decide which months a build touches, whether a release
        # calls itself complete, and where its history starts. A change there moves the
        # release's meaning without moving a single Parquet byte, so a build made under
        # the old rules must not carry into one made under the new ones.
        "implementation_sha256": {
            "market/lake/datasets.py": semantic_source_digest(
                Path(__file__).with_name("datasets.py")
            ),
            "market/lake/writer.py": semantic_source_digest(Path(__file__)),
            "market/sqlite/coverage.py": semantic_source_digest(
                Path(__file__).resolve().parents[1] / "sqlite" / "coverage.py"
            ),
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
