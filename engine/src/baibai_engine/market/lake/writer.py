"""Deterministic legacy SQLite to L1 Canonical Parquet export."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

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


@dataclass(frozen=True)
class LakeBuildReport:
    manifest_path: Path
    manifest: DatasetManifest
    partitions: int
    """How many partitions this build derived — every one the store holds."""
    new_objects: int
    """How many content-addressed keys the build installed that the mirror lacked."""


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
    source_snapshot: LegacySQLiteSnapshot,
    build_id: str | None = None,
    created_at: datetime | None = None,
) -> LakeBuildReport:
    """Derive every partition of one dataset from the sealed snapshot.

    The sealed snapshot is passed in rather than captured here: it belongs to the
    operation, and one export writes every dataset from one seal.

    Nothing is carried from an earlier build. An object is addressed by the digest of
    its own bytes, so a partition whose rows did not move lands on the key it already
    has and costs no new object; what a full derivation buys is that the manifest is
    proved against SQLite in whole on every run, with no notion of a base whose
    generation could disagree with this one.
    """

    dataset = require_lake_dataset(dataset_name)
    now = (created_at or datetime.now(UTC)).astimezone(UTC)
    actual_build_id = build_id or f"{now:%Y%m%dT%H%M%SZ}-legacy-{uuid.uuid4().hex[:8]}"
    validate_identifier(actual_build_id, label="build_id")
    staging_root = _workspace_path(mirror_root, "staging", actual_build_id)
    if staging_root.exists():
        raise LakeBuildError(f"build workspace already exists: {actual_build_id}")
    snapshot = source_snapshot
    staging_root.mkdir(parents=True)

    try:
        with _open_immutable(snapshot.path) as connection:
            _validate_sqlite_contract(connection, dataset)
            periods = _selected_periods(connection, dataset)
            if not periods:
                raise LakeBuildError("dataset holds no rows")
            built = [
                _build_period(
                    dataset=dataset,
                    period=period,
                    staging_root=staging_root,
                    rows=_period_rows(connection, dataset, period),
                    sources=(snapshot.ref,),
                )
                for period in periods
            ]
            data_as_of = _data_as_of(connection, dataset, periods=periods)
            coverage_status, coverage_start, population_count = _coverage_assessment(
                connection, dataset
            )

        ordered = tuple(item.manifest for item in built)
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
            created_at=now,
            coverage_start=coverage_start,
            data_as_of=data_as_of,
            population_count=population_count,
            coverage_status=coverage_status,
            partition_by=dataset.partition_by,
            partitions=ordered,
            totals=totals,
        )
        new_objects = _promote_partitions(mirror_root, built)
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
            partitions=len(built),
            new_objects=new_objects,
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
    created_at: datetime | None = None,
) -> LakeExportReport:
    """Export every lake dataset from one sealed SQLite generation."""
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
                source_snapshot=snapshot,
                created_at=created_at,
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
) -> None:
    """Check that the manifest describes the same periods SQLite holds, and their rows.

    The period inventory is compared first: a period present in one side and absent
    from the other is a hole no per-partition check would look at, and answering it
    costs one query. Every partition's rows are then compared in full.
    """
    dataset = require_lake_dataset(manifest.dataset)
    with _open_immutable(sqlite_path) as connection:
        _validate_sqlite_contract(connection, dataset)
        source_periods = set(_selected_periods(connection, dataset))
        partitions = _manifest_partitions(manifest)
        if source_periods != set(partitions):
            raise LakeBuildError("SQLite and manifest partition inventories differ")
        for period, partition in partitions.items():
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


def _build_period(
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
        ),
    )


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


def _selected_periods(connection: sqlite3.Connection, dataset: LakeDataset) -> tuple[Period, ...]:
    parts = _period_sql_parts(dataset)
    query = (
        f"SELECT DISTINCT {', '.join(parts)} FROM {dataset.sqlite_table} "  # nosec B608
        f"ORDER BY {', '.join(str(index) for index in range(1, len(parts) + 1))}"
    )
    return tuple(tuple(int(part) for part in row) for row in connection.execute(query))


def _period_sql_parts(dataset: LakeDataset) -> tuple[str, ...]:
    """The ISO date substrings that name one partition, in layout order.

    ISO dates sort and slice as text, so the grain is a prefix length rather than a date
    function: the same expression names the partition and orders the result.
    """

    year = f"substr({dataset.date_column}, 1, 4)"
    if dataset.partition_grain == "year":
        return (year,)
    return (year, f"substr({dataset.date_column}, 6, 2)")


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


def _manifest_partitions(manifest: DatasetManifest) -> dict[Period, PartitionManifest]:
    """Key each partition by the calendar period its own manifest layout names.

    The layout is read from the manifest rather than the dataset contract so that a
    manifest written under a different grain is keyed by what it actually says. The
    reader refuses that manifest separately; keying it by today's contract instead would
    silently reinterpret its partitions first.
    """

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


@contextmanager
def _open_immutable(path: Path) -> Iterator[sqlite3.Connection]:
    with closing(
        sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    ) as connection:
        connection.execute("PRAGMA case_sensitive_like=ON")
        yield connection


def _pk_indexes(dataset: LakeDataset) -> tuple[int, ...]:
    names = tuple(column.name for column in dataset.columns)
    return tuple(names.index(name) for name in dataset.primary_key)


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
