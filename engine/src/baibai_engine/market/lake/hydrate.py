"""Fill a market SQLite store's lake-owned tables from one fixed release, and empty them.

The market store keeps being written by ingest after it is filled, so it must carry the
market schema itself — its write-time guards and its indexes — rather than a shape
derived from the dataset contract. The two differ: the store constrains ``week_end`` and
carries secondary indexes the contract does not declare, and a store built to the
contract shape would silently accept rows the real one rejects.

So the fill works from the store outwards. A sealed copy of the store supplies the schema
and every table the lake does not own, and each lake-owned table is emptied and reloaded
from the release's objects. The copy is promoted with a single rename, so a reader never
sees a half-filled store and a failed fill leaves the previous one intact.

Filling is fail-closed on the count: a store whose lake tables silently stayed empty
would let a screening run publish an empty universe as a healthy result, which is the
one new failure the daily lake cycle introduces. The loaded rows must equal what the
release manifest published, per dataset.

Emptying is fail-closed on the same count, from the other side. The copy that goes to
R2 carries no row the lake owns, and "the lake owns it" has to mean the release actually
holds those rows — not that the dataset is declared. A dataset that is in the registry
but not yet in a release (a source whose publication has not started) would otherwise be
emptied into nothing on the first day it has rows.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..sqlite.snapshot import create_snapshot, validate_snapshot
from .datasets import LAKE_DATASETS, LakeDataset
from .duck import LakeSession
from .models import PartitionManifest
from .objects import LakeObjectCache, TransferAccounting
from .reader import (
    FixedRelease,
    accepted_dataset,
    iter_partition_rows,
    materialize_partitions,
    partition_objects,
    selected_partitions,
)
from .retention import exclusive_lock

_MIN_FREE_BYTES = 64 * 1024 * 1024
# SQLite holds the same rows in b-trees with the store's own indexes, so a store is a
# multiple of the compressed Parquet the release publishes. The production release is
# 299,949,710 bytes of Parquet against a 2,013,155,328-byte store — 6.71 — and the
# factor is set above that so the check refuses before the load rather than after it.
_STORE_EXPANSION_FACTOR = 8


class LakeHydrateError(RuntimeError):
    """The market store cannot be filled from the release, or emptied against it."""


@dataclass(frozen=True, slots=True)
class PlannedDataset:
    dataset: str
    build_id: str
    contract_version: int
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class PlannedObject:
    dataset: str
    object_key: str
    sha256: str
    bytes: int
    rows: int


@dataclass(frozen=True, slots=True)
class ReleaseLoadPlan:
    """Exactly which objects a fill would read, and how many rows they must produce."""

    source_release_id: str
    source_release_manifest_sha256: str
    data_as_of: date
    datasets: tuple[PlannedDataset, ...]
    objects: tuple[PlannedObject, ...]


def plan_release_load(
    release: FixedRelease,
    *,
    dataset_names: Sequence[str],
) -> tuple[ReleaseLoadPlan, dict[str, tuple[PartitionManifest, ...]]]:
    """Resolve the exact objects a fill would read, before reading any of them."""

    if not dataset_names:
        raise LakeHydrateError("a load plan must contain at least one dataset")
    if len(set(dataset_names)) != len(dataset_names):
        raise LakeHydrateError("a load plan cannot list the same dataset twice")
    datasets = [accepted_dataset(release, name) for name in sorted(dataset_names)]
    partitions: dict[str, tuple[PartitionManifest, ...]] = {}
    entries: list[PlannedDataset] = []
    objects: list[PlannedObject] = []
    for dataset in datasets:
        selected = selected_partitions(release, dataset.name)
        if not selected:
            raise LakeHydrateError(f"release publishes no partition for {dataset.name}")
        partitions[dataset.name] = selected
        manifest = release.dataset_manifest(dataset.name)
        entries.append(
            PlannedDataset(
                dataset=dataset.name,
                build_id=manifest.build_id,
                contract_version=manifest.contract_version,
                manifest_sha256=release.dataset_manifest_sha256[dataset.name],
            )
        )
        objects.extend(
            PlannedObject(
                dataset=dataset.name,
                object_key=item.key,
                sha256=item.sha256,
                bytes=item.bytes,
                rows=item.rows,
            )
            for item in partition_objects(selected)
        )
    plan = ReleaseLoadPlan(
        source_release_id=release.release_id,
        source_release_manifest_sha256=release.manifest_sha256,
        data_as_of=release.data_as_of,
        datasets=tuple(sorted(entries, key=lambda item: item.dataset)),
        objects=tuple(sorted(objects, key=lambda item: (item.dataset, item.object_key))),
    )
    return plan, partitions


def expected_row_totals(plan: ReleaseLoadPlan) -> dict[str, int]:
    expected: dict[str, int] = {}
    for item in plan.objects:
        expected[item.dataset] = expected.get(item.dataset, 0) + item.rows
    return dict(sorted(expected.items()))


@dataclass(frozen=True, slots=True)
class HydrateReport:
    path: Path
    release_id: str
    release_manifest_sha256: str
    data_as_of: date
    schema_version: int
    rows: Mapping[str, int]
    transfers: TransferAccounting

    def as_dict(self) -> dict[str, object]:
        return {
            "data_as_of": self.data_as_of.isoformat(),
            "release_id": self.release_id,
            "release_manifest_sha256": self.release_manifest_sha256,
            "rows": dict(sorted(self.rows.items())),
            "schema_version": self.schema_version,
            "store": str(self.path),
            **self.transfers.as_dict(),
        }


def hydrate_market_store(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    store: Path,
    dataset_names: Sequence[str],
    still_current: Callable[[], tuple[str, str]] | None = None,
) -> HydrateReport:
    """Replace the lake-owned tables of ``store`` with the contents of one release.

    A caller that asked for "the current release" resolved the pointer before taking the
    lock, so the fill re-asks under it through ``still_current`` and refuses rather than
    installing a generation the pointer has already moved past.
    """

    if not store.is_file():
        raise LakeHydrateError(f"market store does not exist: {store}")
    plan, partitions = plan_release_load(release, dataset_names=dataset_names)
    require_still_current(still_current, release)
    with exclusive_lock(store.with_name(f".{store.name}.lock"), subject="market store"):
        require_durable_filesystem(store.parent)
        require_free_capacity(store.parent, store=store, plan=plan)
        schema_version = validate_snapshot(store)
        before = cache.transfers.as_dict()
        temporary = store.with_name(f".{store.name}.{os.getpid()}.{uuid.uuid4().hex}.hydrating")
        rows: dict[str, int] = {}
        try:
            create_snapshot(store, temporary)
            connection = sqlite3.connect(temporary)
            try:
                connection.execute("PRAGMA journal_mode=OFF")
                for name in sorted(partitions):
                    dataset = accepted_dataset(release, name)
                    _require_contract_shape(connection, dataset)
                    # The store's own DDL is what gets restored, so the fill cannot
                    # invent an index the schema does not declare or lose one it does.
                    # Loading into an indexed table costs several times the load itself.
                    indexes = _detach_secondary_indexes(connection, dataset)
                    connection.execute(f"DELETE FROM {dataset.sqlite_table}")  # nosec B608
                    rows[name] = load_dataset_rows(
                        connection,
                        session=session,
                        cache=cache,
                        dataset=dataset,
                        partitions=partitions[name],
                    )
                    for statement in indexes:
                        connection.execute(statement)
                connection.commit()
                if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise LakeHydrateError("hydrated market store failed quick_check")
            finally:
                connection.close()
            _require_published_rows(rows, expected_row_totals(plan))
            filled_version = validate_snapshot(temporary)
            if filled_version != schema_version:
                raise LakeHydrateError(
                    f"hydration moved the market schema version from {schema_version} "
                    f"to {filled_version}"
                )
            require_still_current(still_current, release)
            durable_replace(temporary, store)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    return HydrateReport(
        path=store,
        release_id=release.release_id,
        release_manifest_sha256=release.manifest_sha256,
        data_as_of=release.data_as_of,
        schema_version=schema_version,
        rows=dict(sorted(rows.items())),
        transfers=cache.transfers.since(before),
    )


@dataclass(frozen=True, slots=True)
class DehydrateReport:
    path: Path
    release_id: str
    removed_rows: Mapping[str, int]
    bytes_before: int
    bytes_after: int

    def as_dict(self) -> dict[str, object]:
        return {
            "bytes_after": self.bytes_after,
            "bytes_before": self.bytes_before,
            "release_id": self.release_id,
            "removed_rows": dict(sorted(self.removed_rows.items())),
            "store": str(self.path),
        }


def dehydrate_market_store(store: Path, *, release: FixedRelease) -> DehydrateReport:
    """Empty every lake-owned table of ``store``, keeping only what the lake does not hold.

    This runs on the copy that is about to be uploaded, never on the working store. It
    refuses unless the release accounts for every row it is about to drop, so the only
    way to shrink the published object is to have published the rows first.
    """

    if not store.is_file():
        raise LakeHydrateError(f"market store does not exist: {store}")
    published = expected_row_totals(
        plan_release_load(release, dataset_names=tuple(sorted(release.dataset_manifests)))[0]
    )
    bytes_before = store.stat().st_size
    removed: dict[str, int] = {}
    connection = sqlite3.connect(store)
    try:
        for name, dataset in sorted(LAKE_DATASETS.items()):
            held = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {dataset.sqlite_table}"  # nosec B608
                ).fetchone()[0]
            )
            if name not in release.dataset_manifests:
                if held:
                    raise LakeHydrateError(
                        f"{name} holds {held} row(s) the release does not publish; "
                        "emptying it would drop rows no release can restore"
                    )
                continue
            if held != published.get(name, 0):
                raise LakeHydrateError(
                    f"{name} holds {held} row(s) but release {release.release_id} "
                    f"publishes {published.get(name, 0)}; publish before emptying"
                )
            connection.execute(f"DELETE FROM {dataset.sqlite_table}")  # nosec B608
            removed[name] = held
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    return DehydrateReport(
        path=store,
        release_id=release.release_id,
        removed_rows=dict(sorted(removed.items())),
        bytes_before=bytes_before,
        bytes_after=store.stat().st_size,
    )


def require_still_current(
    still_current: Callable[[], tuple[str, str]] | None, release: FixedRelease
) -> None:
    """The full identity, not the name: an ID can be reused for different bytes.

    A recovery tool that republishes a manifest under an existing release ID would pass
    a name comparison while pointing at a different graph, and the store filled from the
    old bytes would take the destination.
    """

    if still_current is None:
        return
    actual = still_current()
    expected = (release.release_id, release.manifest_sha256)
    if actual != expected:
        raise LakeHydrateError(
            f"the current release moved while this store was being filled: "
            f"{expected[0]} is no longer current ({actual[0]} is)"
        )


def require_free_capacity(parent: Path, *, store: Path, plan: ReleaseLoadPlan) -> None:
    """Refuse before the load unless the filled copy can fit beside the current store.

    The fill writes one temporary copy: it starts as a byte copy of the store and ends
    holding every published row, so the peak it needs is the larger of the two rather
    than their sum.
    """

    published_bytes = sum(item.bytes for item in plan.objects)
    required = max(
        _MIN_FREE_BYTES,
        store.stat().st_size,
        published_bytes * _STORE_EXPANSION_FACTOR,
    )
    if shutil.disk_usage(parent).free < required:
        raise LakeHydrateError(f"hydration requires at least {required} free bytes in {parent}")


def require_durable_filesystem(parent: Path) -> None:
    """Fail before the expensive load unless publication primitives are supported."""
    token = uuid.uuid4().hex
    upper = parent / f".hydrate-{token}-Case.probe"
    lower = parent / f".hydrate-{token}-case.probe"
    linked = parent / f".hydrate-{token}-linked.probe"
    renamed = parent / f".hydrate-{token}-renamed.probe"
    directory = -1
    try:
        for path in (upper, lower):
            with path.open("xb") as probe:
                probe.write(b"market-store-filesystem-probe")
                probe.flush()
                os.fsync(probe.fileno())
        if upper.samefile(lower):
            raise LakeHydrateError("the market store requires a case-sensitive filesystem")
        linked.hardlink_to(lower)
        if not linked.samefile(lower):
            raise LakeHydrateError("market store filesystem does not preserve hard-link identity")
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        os.fsync(directory)
        upper.replace(renamed)
        os.fsync(directory)
    except LakeHydrateError:
        raise
    except OSError as exc:
        raise LakeHydrateError(
            "market store filesystem does not support durable same-directory publication"
        ) from exc
    finally:
        if directory >= 0:
            os.close(directory)
        for path in (upper, lower, linked, renamed):
            with suppress(OSError):
                path.unlink(missing_ok=True)


def durable_replace(temporary: Path, destination: Path) -> None:
    with temporary.open("rb") as source:
        os.fsync(source.fileno())
    rollback = destination.with_name(
        f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.rollback"
    )
    try:
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise LakeHydrateError(
            "market store filesystem does not support directory durability"
        ) from exc
    try:
        # Prove this filesystem supports directory fsync before changing the
        # visible destination. The second fsync makes the rename durable.
        os.fsync(directory)
        had_previous = destination.exists()
        if had_previous:
            rollback.hardlink_to(destination)
            os.fsync(directory)
        try:
            temporary.replace(destination)
        except BaseException:
            rollback.unlink(missing_ok=True)
            os.fsync(directory)
            raise
        try:
            os.fsync(directory)
        except OSError as exc:
            try:
                if had_previous:
                    rollback.replace(destination)
                else:
                    destination.unlink(missing_ok=True)
                os.fsync(directory)
            except OSError as recovery_error:
                raise LakeHydrateError(
                    "market store publication durability failed and rollback could not be proven"
                ) from recovery_error
            raise LakeHydrateError(
                "market store publication durability failed; the previous generation was restored"
            ) from exc
        with suppress(OSError):
            rollback.unlink(missing_ok=True)
            # The new destination is already durable. A retained hidden hard link
            # does not change the store path or its contents.
    finally:
        os.close(directory)


def load_dataset_rows(
    connection: sqlite3.Connection,
    *,
    session: LakeSession,
    cache: LakeObjectCache,
    dataset: LakeDataset,
    partitions: Sequence[PartitionManifest],
) -> int:
    """Load one dataset a partition at a time, in bounded row batches.

    One partition is one month, and its objects are fetched and verified just before
    they are read, so neither the objects nor the rows of the whole dataset are ever
    held at once. SQLite stores the table in its primary-key B-tree, so the order
    partitions arrive in does not change the result.
    """

    names = tuple(column.name for column in dataset.columns)
    statement = (
        f"INSERT INTO {dataset.sqlite_table}({', '.join(names)}) "  # nosec B608
        f"VALUES ({', '.join('?' for _ in names)})"
    )
    loaded = 0
    for partition in partitions:
        paths = materialize_partitions(cache, dataset=dataset, partitions=(partition,))
        for batch in iter_partition_rows(session, dataset=dataset, paths=paths, columns=names):
            connection.executemany(statement, batch)
            loaded += len(batch)
    return loaded


def _require_contract_shape(connection: sqlite3.Connection, dataset: LakeDataset) -> None:
    """Refuse a store whose table has drifted from the dataset contract.

    The contract was derived from this schema, so the two agreeing is the premise of
    the whole fill. Comparing the ordered names rather than the set is deliberate: a
    reordered column reaches the same insert (which names its columns) but means the
    schema moved without a contract bump, and that is exactly what has to be caught
    before a build publishes objects under the unchanged contract version.
    """

    actual = tuple(
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({dataset.sqlite_table})")
    )
    expected = tuple(column.name for column in dataset.columns)
    if actual != expected:
        raise LakeHydrateError(
            f"market table {dataset.sqlite_table} does not match the {dataset.name} "
            f"contract: store has {actual}, contract declares {expected}"
        )


def _detach_secondary_indexes(
    connection: sqlite3.Connection, dataset: LakeDataset
) -> tuple[str, ...]:
    """Drop the table's explicit indexes and return the DDL that recreates them.

    Indexes SQLite created for a primary key or a unique constraint carry no DDL and
    cannot be dropped; they stay, which is what keeps the load's key semantics intact.
    """

    declared = tuple(
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL "
            "ORDER BY name",
            (dataset.sqlite_table,),
        )
    )
    for name, _ in declared:
        connection.execute(f"DROP INDEX {name}")
    return tuple(statement for _, statement in declared)


def _require_published_rows(loaded: Mapping[str, int], published: Mapping[str, int]) -> None:
    for name in sorted(set(loaded) | set(published)):
        if loaded.get(name, 0) != published.get(name, 0):
            raise LakeHydrateError(
                f"hydration loaded {loaded.get(name, 0)} row(s) for {name}; "
                f"the release manifest published {published.get(name, 0)}"
            )
