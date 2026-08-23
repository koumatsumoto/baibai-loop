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

import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType

import duckdb
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from .datasets import LAKE_DATASETS, LakeDataset, period_label, require_lake_dataset
from .duck import LakeSession
from .keys import (
    current_l1_pointer_key,
    dataset_manifest_key,
    release_manifest_key,
    validate_identifier,
)
from .models import (
    DatasetManifest,
    L1ReleaseSourceRef,
    LakeObject,
    PartitionManifest,
    ReleaseManifest,
    load_lake_model_json,
    validate_release_policy,
)
from .objects import LakeObjectCache, LakeObjectSource, sha256_bytes
from .release import L1ReleasePointer
from .writer import affected_periods


class LakeReadError(RuntimeError):
    """The lake graph does not resolve to one internally consistent release."""


# How many rows are converted into Python objects at once. Peak memory follows this
# number, not the dataset: a decade of daily bars is eight figures of rows.
ROW_BATCH_SIZE = 20_000

type Month = tuple[int, ...]


@dataclass(frozen=True, slots=True)
class FixedRelease:
    """One immutable input generation, resolved once and reused for a whole run."""

    release_id: str
    manifest_key: str
    manifest_sha256: str
    data_as_of: date
    manifest: ReleaseManifest
    dataset_manifests: Mapping[str, DatasetManifest]
    dataset_manifest_sha256: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "dataset_manifests",
            MappingProxyType(dict(self.dataset_manifests)),
        )
        object.__setattr__(
            self,
            "dataset_manifest_sha256",
            MappingProxyType(dict(self.dataset_manifest_sha256)),
        )

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


def resolve_current_release(source: LakeObjectSource, *, evaluated_at: datetime) -> FixedRelease:
    """Freeze current and require it to satisfy policy at the operational read time."""

    pointer = _read_current_pointer(source)
    release = _load_release(
        source,
        release_id=pointer.release_id,
        expected_manifest_sha256=pointer.manifest_sha256,
    )
    try:
        validate_release_policy(
            release.manifest,
            release.dataset_manifests,
            evaluated_at=evaluated_at,
        )
    except ValueError as exc:
        raise LakeReadError(f"L1 current release fails operational policy: {exc}") from None
    return release


def _read_current_pointer(source: LakeObjectSource) -> L1ReleasePointer:
    try:
        payload = source.read_bytes(current_l1_pointer_key())
    except Exception as exc:
        # The pointer is written by publication, so a mirror that has never published
        # does not have one. Reporting that as an unreadable object sends the reader
        # looking for a corrupt file that was never there — and it is the first thing a
        # local build hits, because a release created locally is named explicitly until
        # it is published.
        raise LakeReadError(
            "L1 current pointer is absent; publish a release, or name one with "
            "--release and --manifest-sha256"
        ) from exc
    try:
        pointer = load_lake_model_json(payload, L1ReleasePointer)
    except ValueError:
        raise LakeReadError("L1 current pointer is invalid") from None
    expected_key = release_manifest_key(release_id=pointer.release_id)
    if pointer.manifest_key != expected_key:
        raise LakeReadError("L1 current pointer manifest key does not match its release id")
    return pointer


def resolve_release(
    source: LakeObjectSource,
    release_id: str,
    *,
    manifest_sha256: str,
) -> FixedRelease:
    """Resolve one named release without reading the pointer at all.

    This is how a release that is not current gets read: the release is chosen by the
    caller, so nothing about the current pointer takes part in the decision. A local
    mirror has no pointer until it publishes, so it is also the only way to read one
    before then.
    """

    validate_identifier(release_id, label="release_id")
    return _load_release(
        source,
        release_id=release_id,
        expected_manifest_sha256=manifest_sha256,
    )


def resolve_release_ref(source: LakeObjectSource, reference: L1ReleaseSourceRef) -> FixedRelease:
    """Resolve a persisted reference whose typed identity includes the manifest digest."""

    return resolve_release(
        source,
        reference.source_id,
        manifest_sha256=reference.sha256,
    )


def _load_release(
    source: LakeObjectSource,
    *,
    release_id: str,
    expected_manifest_sha256: str,
) -> FixedRelease:
    manifest_key = release_manifest_key(release_id=release_id)
    payload = source.read_bytes(manifest_key)
    digest = sha256_bytes(payload)
    if digest != expected_manifest_sha256:
        raise LakeReadError("L1 release manifest digest does not match its expected identity")
    try:
        release = load_lake_model_json(payload, ReleaseManifest)
    except ValueError:
        raise LakeReadError("L1 release manifest is invalid") from None
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
            manifest = load_lake_model_json(dataset_payload, DatasetManifest)
        except ValueError:
            raise LakeReadError(f"dataset manifest is invalid: {name}") from None
        # Everything the release entry restates about the manifest, not only what
        # addresses it. Freshness is rightly exempted on a historical read — a pinned
        # study is old on purpose — but the watermark, coverage, and totals a release
        # states are what a reader reports about the generation, and an entry that
        # disagrees with the manifest it names describes data that is not there.
        if (
            manifest.dataset != name
            or manifest.build_id != entry.build_id
            or manifest.contract_version != entry.contract_version
            or manifest.data_as_of != entry.data_as_of
            or manifest.coverage_status != entry.coverage_status
            or manifest.totals != entry.totals
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
        data_as_of=release.data_as_of,
        manifest=release,
        dataset_manifests=manifests,
        dataset_manifest_sha256=digests,
    )


def accepted_dataset(release: FixedRelease, dataset_name: str) -> LakeDataset:
    """The dataset contract this reader accepts, or an error naming the mismatch."""

    dataset = require_lake_dataset(dataset_name)
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
    periods: Iterable[Month] | None = None,
) -> tuple[PartitionManifest, ...]:
    """Partitions of one dataset, in calendar order, restricted to ``periods``."""

    manifest = release.dataset_manifest(dataset_name)
    layout = tuple(manifest.partition_by)
    wanted = set(periods) if periods is not None else None

    def key(partition: PartitionManifest) -> Month:
        return partition_period(partition, layout)

    selected = [
        partition for partition in manifest.partitions if wanted is None or key(partition) in wanted
    ]
    if wanted is not None:
        missing = sorted(wanted - {key(partition) for partition in selected})
        if missing:
            rendered = ", ".join(period_label(item) for item in missing)
            raise LakeReadError(
                f"release {release.release_id} does not publish {dataset_name} for {rendered}"
            )
    return tuple(sorted(selected, key=key))


def partition_period(partition: PartitionManifest, layout: tuple[str, ...]) -> Month:
    """Read one partition's calendar position through the layout its manifest declares.

    The layout comes from the manifest rather than the dataset contract so that a
    manifest written under another grain is read as what it says. Resolving it against
    today's contract instead would reinterpret its partitions before anything checked
    whether the manifest is acceptable at all.
    """

    return tuple(int(partition.values[name]) for name in layout)


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


def release_backing_store(
    source: LakeObjectSource,
    *,
    store: Path,
    release_id: str,
    manifest_sha256: str,
) -> L1ReleaseSourceRef | None:
    """The named release, when the store demonstrably holds exactly what it publishes.

    A cohort may state a release as its lineage only if that release can give the rows
    back. Naming one is not that: a store filled from an older generation, or carrying
    fetches nobody published, reads the same from the outside. So the name is a hint and
    the proof is here — every lake-owned partition is compared with the same row and
    coverage identity the publisher uses, and the reference is returned only when all
    of them agree exactly.

    The current pointer is not consulted. It lives in the object store rather than the
    local mirror, and a build that reached for it would be doing network I/O to answer a
    question the store in front of it already settles. A release that is no longer
    current still reproduces the rows a cohort read from it.

    A dataset the release omits must hold nothing. That is how a dataset with no rows
    yet — `jquants.all_issues_daily_margin` until the exchange starts publishing it —
    stays consistent, while a table holding rows no release carries makes the claim
    false rather than approximate.

    Absent rather than raising. A store that does not match any release is the ordinary
    state during a backfill, and the honest consequence is a cohort whose assurance stays
    `trace_only` — not a build that refuses to run.
    """

    try:
        release = resolve_release(source, release_id, manifest_sha256=manifest_sha256)
    except LakeReadError:
        return None
    try:
        with closing(sqlite3.connect(f"{store.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            for name, dataset in LAKE_DATASETS.items():
                manifest = release.dataset_manifests.get(name)
                if manifest is not None:
                    if affected_periods(conn, dataset, manifest):
                        return None
                    continue
                held = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM {dataset.sqlite_table}"  # nosec B608
                    ).fetchone()[0]
                )
                if held:
                    return None
    except sqlite3.Error:
        return None
    return L1ReleaseSourceRef(
        kind="l1_release",
        source_id=release.release_id,
        key=release.manifest_key,
        sha256=release.manifest_sha256,
        manifest_version=release.manifest.manifest_version,
    )
