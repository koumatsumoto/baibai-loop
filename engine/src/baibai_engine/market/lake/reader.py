"""Resolve one release at the start of a run, then read only that release.

The current pointer is the single mutable object in the lake. It is read exactly
once, at ``resolve_current_release``; everything downstream travels as a
``FixedRelease`` holding immutable keys and digests. A publisher that switches the
pointer while a run is in flight therefore cannot change what that run reads.

Reads are fail-closed on identity, and the identity chain is closed by digest at
every edge: the release manifest must hash to what the pointer named, each dataset
manifest must hash to what the release entry named, and each object must hash to
what its dataset manifest named while also carrying the schema, contract, row
count, and key range that manifest published. A reader accepts exactly one
``contract_version`` per dataset; a different contract is an error rather than
something to adapt to.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from .datasets import LakeDataset, require_pilot_dataset
from .duck import LakeSession
from .keys import (
    current_l1_pointer_key,
    dataset_manifest_key,
    release_manifest_key,
    validate_identifier,
)
from .models import DatasetManifest, LakeObject, PartitionManifest, ReleaseManifest
from .objects import LakeObjectCache, LakeObjectSource, sha256_bytes
from .release import L1ReleasePointer


class LakeReadError(RuntimeError):
    """The lake graph does not resolve to one internally consistent release."""


# How many rows are converted into Python objects at once. Peak memory follows this
# number, not the dataset: a decade of daily bars is eight figures of rows.
ROW_BATCH_SIZE = 20_000

type Month = tuple[int, int]


@dataclass(frozen=True, slots=True)
class FixedRelease:
    """One immutable input generation, resolved once and reused for a whole run."""

    release_id: str
    manifest_key: str
    manifest_sha256: str
    previous_release_id: str | None
    data_as_of: date
    manifest: ReleaseManifest
    dataset_manifests: Mapping[str, DatasetManifest]
    dataset_manifest_sha256: Mapping[str, str]

    def dataset_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.dataset_manifests))

    def dataset_manifest(self, dataset: str) -> DatasetManifest:
        try:
            return self.dataset_manifests[dataset]
        except KeyError:
            available = ", ".join(self.dataset_names())
            raise LakeReadError(
                f"release {self.release_id} does not contain dataset {dataset!r}; "
                f"published datasets: {available or '(none)'}"
            ) from None


def resolve_current_release(source: LakeObjectSource) -> FixedRelease:
    """Read the mutable pointer once and freeze what it names."""

    payload = source.read_bytes(current_l1_pointer_key())
    try:
        pointer = L1ReleasePointer.model_validate_json(payload)
    except ValueError as exc:
        raise LakeReadError(f"L1 current pointer is invalid: {exc}") from exc
    expected_key = release_manifest_key(release_id=pointer.release_id)
    if pointer.manifest_key != expected_key:
        raise LakeReadError("L1 current pointer manifest key does not match its release id")
    return _load_release(
        source,
        release_id=pointer.release_id,
        expected_manifest_sha256=pointer.manifest_sha256,
        previous_release_id=pointer.previous_release_id,
    )


def resolve_release(source: LakeObjectSource, release_id: str) -> FixedRelease:
    """Resolve one named release without reading the pointer at all.

    This is how a rollback or a pinned study reads: the release is chosen by the
    caller, so nothing about the current pointer takes part in the decision.
    """

    validate_identifier(release_id, label="release_id")
    return _load_release(
        source,
        release_id=release_id,
        expected_manifest_sha256=None,
        previous_release_id=None,
    )


def _load_release(
    source: LakeObjectSource,
    *,
    release_id: str,
    expected_manifest_sha256: str | None,
    previous_release_id: str | None,
) -> FixedRelease:
    manifest_key = release_manifest_key(release_id=release_id)
    payload = source.read_bytes(manifest_key)
    digest = sha256_bytes(payload)
    if expected_manifest_sha256 is not None and digest != expected_manifest_sha256:
        raise LakeReadError("L1 release manifest digest does not match the current pointer")
    try:
        release = ReleaseManifest.model_validate_json(payload)
    except ValueError as exc:
        raise LakeReadError(f"L1 release manifest is invalid: {exc}") from exc
    if release.release_id != release_id:
        raise LakeReadError("L1 release manifest identifies a different release")

    manifests: dict[str, DatasetManifest] = {}
    digests: dict[str, str] = {}
    for name, entry in sorted(release.datasets.items()):
        key = dataset_manifest_key(dataset=name, build_id=entry.build_id)
        dataset_payload = source.read_bytes(key)
        dataset_digest = sha256_bytes(dataset_payload)
        if dataset_digest != entry.manifest_sha256:
            raise LakeReadError(f"dataset manifest digest does not match the release: {name}")
        try:
            manifest = DatasetManifest.model_validate_json(dataset_payload)
        except ValueError as exc:
            raise LakeReadError(f"dataset manifest is invalid: {name}: {exc}") from exc
        if (
            manifest.dataset != name
            or manifest.build_id != entry.build_id
            or manifest.contract_version != entry.contract_version
        ):
            raise LakeReadError(f"release and dataset manifest disagree: {name}")
        if manifest.layer != "l1_canonical":
            raise LakeReadError(f"L1 release references a non-canonical dataset build: {name}")
        manifests[name] = manifest
        digests[name] = dataset_digest
    return FixedRelease(
        release_id=release_id,
        manifest_key=manifest_key,
        manifest_sha256=digest,
        previous_release_id=previous_release_id,
        data_as_of=release.data_as_of,
        manifest=release,
        dataset_manifests=manifests,
        dataset_manifest_sha256=digests,
    )


def accepted_dataset(release: FixedRelease, dataset_name: str) -> LakeDataset:
    """The dataset contract this reader accepts, or an error naming the mismatch."""

    dataset = require_pilot_dataset(dataset_name)
    manifest = release.dataset_manifest(dataset_name)
    if manifest.contract_version != dataset.contract_version:
        raise LakeReadError(
            f"dataset {dataset_name} publishes contract v{manifest.contract_version}; "
            f"this reader accepts only v{dataset.contract_version}"
        )
    if tuple(manifest.partition_by) != dataset.partition_by:
        raise LakeReadError(f"dataset {dataset_name} publishes an unexpected partition layout")
    return dataset


def selected_partitions(
    release: FixedRelease,
    dataset_name: str,
    *,
    months: Iterable[Month] | None = None,
) -> tuple[PartitionManifest, ...]:
    """Partitions of one dataset, ordered by month, restricted to ``months``."""

    manifest = release.dataset_manifest(dataset_name)
    wanted = set(months) if months is not None else None
    selected = [
        partition
        for partition in manifest.partitions
        if wanted is None or partition_month(partition) in wanted
    ]
    if wanted is not None:
        missing = sorted(wanted - {partition_month(partition) for partition in selected})
        if missing:
            rendered = ", ".join(f"{year:04d}-{month:02d}" for year, month in missing)
            raise LakeReadError(
                f"release {release.release_id} does not publish {dataset_name} for {rendered}"
            )
    return tuple(sorted(selected, key=partition_month))


def partition_month(partition: PartitionManifest) -> Month:
    return (int(partition.values["year"]), int(partition.values["month"]))


def partition_objects(partitions: Sequence[PartitionManifest]) -> tuple[LakeObject, ...]:
    return tuple(item for partition in partitions for item in partition.objects)


def schema_matches(actual: object, expected: object) -> bool:
    """Whether a stored schema is the declared contract, columns and identity both.

    For a dataset with only flat columns, Arrow's own comparison is exact and is what
    the L1 reader uses. This looser form exists for datasets carrying a nested column:
    a Parquet round trip renames the child fields of a map, so an object that is in
    fact byte-for-byte the contract would fail an exact comparison. Column names and
    types are still compared, and the ``baibai.*`` identity the writer stamped is
    compared explicitly, so the guarantee that matters — this object is that dataset
    at that contract version — is unchanged.
    """

    if not actual.equals(expected, check_metadata=False):  # type: ignore[attr-defined]
        return False
    stamped = expected.metadata or {}  # type: ignore[attr-defined]
    stored = actual.metadata or {}  # type: ignore[attr-defined]
    return all(stored.get(key) == value for key, value in stamped.items())


def verify_object(path: Path, dataset: LakeDataset, lake_object: LakeObject) -> None:
    """Check a materialized object against the contract before any row is read.

    The comparison is Arrow's exact one, metadata included, so a file with the right
    column names but a different dataset identity or contract version fails here
    rather than being read as the wrong dataset.
    """

    try:
        schema = pq.read_schema(path)
        metadata = pq.read_metadata(path)
    except (OSError, ValueError) as exc:
        # pyarrow raises ArrowInvalid (a ValueError) for a bad footer and ArrowIOError
        # (an OSError) for an unreadable file.
        raise LakeReadError(f"Parquet object is unreadable: {lake_object.key}: {exc}") from exc
    if not schema.equals(dataset.arrow_schema, check_metadata=True):
        raise LakeReadError(f"Parquet object schema does not match the contract: {lake_object.key}")
    if metadata.num_rows != lake_object.rows:
        raise LakeReadError(
            f"Parquet object row count differs from its manifest: {lake_object.key}"
        )


def iter_partition_rows(
    session: LakeSession,
    *,
    dataset: LakeDataset,
    paths: Sequence[Path],
    columns: Sequence[str] | None = None,
    batch_size: int = ROW_BATCH_SIZE,
) -> Iterator[list[tuple[object, ...]]]:
    """Yield bounded batches of named columns from an explicit object list, PK-ordered.

    ``read_parquet`` receives the exact file list the manifest fixed. There is no
    glob, no directory scan, and no ``union_by_name``: a file whose schema differs
    from the others is an error, which is what makes a silent contract drift
    impossible to read as data.

    Rows arrive in batches because a dataset here is a decade of daily bars. Turning
    a whole partition set into Python objects at once costs on the order of a
    kilobyte per row and would exhaust memory long before a build finished. DuckDB
    keeps the result in its own columnar form and ``fetchmany`` converts one batch at
    a time, so peak memory follows ``batch_size`` rather than the dataset size.
    """

    if not paths:
        return
    if batch_size < 1:
        raise LakeReadError("row batch size must be positive")
    selected = (
        tuple(columns) if columns is not None else tuple(column.name for column in dataset.columns)
    )
    known = {column.name for column in dataset.columns}
    unknown = [name for name in selected if name not in known]
    if unknown:
        raise LakeReadError(f"dataset {dataset.name} has no column {unknown[0]!r}")
    projection = ", ".join(_quote_identifier(name) for name in selected)
    order = ", ".join(_quote_identifier(name) for name in dataset.primary_key)
    statement = (
        f"SELECT {projection} FROM read_parquet($files, union_by_name = false) "  # nosec B608
        f"ORDER BY {order}"
    )
    cursor = session.connection.cursor()
    try:
        cursor.execute(statement, {"files": [str(path) for path in paths]})
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                return
            yield batch
    except duckdb.Error as exc:
        raise LakeReadError(
            f"dataset {dataset.name} could not be read: {session.redact(str(exc))}"
        ) from None
    finally:
        cursor.close()


def _quote_identifier(name: str) -> str:
    """Quote a column name that already passed the dataset contract allowlist."""

    if not name.isidentifier():
        raise LakeReadError(f"column name is not a plain identifier: {name!r}")
    return f'"{name}"'


def materialize_partitions(
    cache: LakeObjectCache,
    *,
    dataset: LakeDataset,
    partitions: Sequence[PartitionManifest],
) -> tuple[Path, ...]:
    """Fetch (or reuse) every object of the selected partitions and verify it."""

    paths: list[Path] = []
    for lake_object in partition_objects(partitions):
        path = cache.materialize(lake_object)
        verify_object(path, dataset, lake_object)
        paths.append(path)
    return tuple(paths)
