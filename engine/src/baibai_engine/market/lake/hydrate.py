"""Fill a market SQLite store's lake-owned tables from one fixed release.

A projection is disposable and read-only; this is not. The market store keeps being
written by ingest after it is filled, so it must carry the market schema itself —
its write-time guards and its indexes — and not the shape a projection derives from
the dataset contract. The two differ: the store constrains ``week_end`` and carries
secondary indexes the contract does not declare, and a store built to the projection
shape would silently accept rows the real one rejects.

So the fill goes the other way round. A sealed copy of the store supplies the schema
and every table the lake does not own, and each lake-owned table is emptied and
reloaded from the release's objects. The copy is promoted with a single rename, so a
reader never sees a half-filled store and a failed fill leaves the previous one
intact.

Filling is fail-closed on the count: a store whose lake tables silently stayed empty
would let a screening run publish an empty universe as a healthy result, which is the
one new failure the cutover introduces. The loaded rows must equal what the release
manifest published, per dataset.

Emptying is fail-closed on the same count, from the other side. The copy that goes to
R2 carries no row the lake owns, and "the lake owns it" has to mean the release
actually holds those rows — not that the dataset is declared. A dataset that is in the
registry but not yet in a release (a source whose publication has not started) would
otherwise be emptied into nothing on the first day it has rows.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..sqlite.snapshot import create_snapshot, validate_snapshot
from .datasets import LAKE_DATASETS, LakeDataset
from .duck import LakeSession
from .objects import LakeObjectCache, TransferAccounting
from .projection import (
    durable_replace,
    expected_row_totals,
    load_dataset_rows,
    plan_identity,
    require_durable_filesystem,
    require_free_capacity,
    require_still_current,
)
from .reader import FixedRelease, accepted_dataset
from .retention import exclusive_lock


class LakeHydrateError(RuntimeError):
    """The market store cannot be filled from the release."""


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

    ``still_current`` carries the same meaning it has for a projection: a caller that
    asked for "the current release" resolved the pointer before taking the lock, so the
    fill re-asks under it and refuses rather than installing a generation the pointer
    has already moved past.
    """

    if not store.is_file():
        raise LakeHydrateError(f"market store does not exist: {store}")
    identity, partitions = plan_identity(release, dataset_names=dataset_names)
    require_still_current(still_current, release)
    with exclusive_lock(store.with_name(f".{store.name}.lock"), subject="market store"):
        require_durable_filesystem(store.parent)
        require_free_capacity(store.parent, identity)
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
            _require_published_rows(rows, expected_row_totals(identity))
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
        plan_identity(release, dataset_names=tuple(sorted(release.dataset_manifests)))[0]
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
