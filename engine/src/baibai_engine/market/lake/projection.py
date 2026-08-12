"""A disposable SQLite projection of one fixed release.

The projection is not an authority. It exists so the operational path keeps the
point-lookup shape SQLite is good at while the release stays the thing that is
published, and it can be deleted at any time because everything in it is derived
from immutable objects.

Reuse is decided by exact identity, never by age or by a version counter: the
release, the release manifest digest, every dataset manifest digest, every object
digest, the projection contract fingerprint, and the producing commit must all
match. Anything else — a differing field, a missing metadata table, an interrupted
build — rebuilds. A build writes to a unique temporary file and is promoted with a
single rename, so a reader never observes a half-built projection and a failed
build leaves the previous one intact.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from .datasets import PILOT_DATASETS, LakeDataset
from .duck import LakeSession
from .models import PartitionManifest
from .objects import LakeObjectCache, TransferAccounting
from .reader import (
    FixedRelease,
    LakeReadError,
    accepted_dataset,
    iter_partition_rows,
    materialize_partitions,
    partition_objects,
    selected_partitions,
)

PROJECTION_CONTRACT_VERSION = 1

_META_SCHEMA = """
CREATE TABLE projection_meta(
  single_row INTEGER NOT NULL PRIMARY KEY CHECK (single_row = 1),
  projection_contract_version INTEGER NOT NULL,
  projection_fingerprint TEXT NOT NULL,
  source_release_id TEXT NOT NULL,
  source_release_manifest_sha256 TEXT NOT NULL,
  producer_git_commit TEXT NOT NULL,
  built_at TEXT NOT NULL,
  data_as_of TEXT NOT NULL
);

CREATE TABLE projection_dataset(
  dataset TEXT NOT NULL PRIMARY KEY,
  build_id TEXT NOT NULL,
  contract_version INTEGER NOT NULL,
  manifest_sha256 TEXT NOT NULL
);

CREATE TABLE projection_object(
  dataset TEXT NOT NULL,
  object_key TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  rows INTEGER NOT NULL,
  PRIMARY KEY (dataset, object_key)
);
"""


class ProjectionError(RuntimeError):
    """The projection cannot be built, or cannot prove which release it holds."""


@dataclass(frozen=True, slots=True)
class ProjectionDataset:
    dataset: str
    build_id: str
    contract_version: int
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class ProjectionObject:
    dataset: str
    object_key: str
    sha256: str
    bytes: int
    rows: int


@dataclass(frozen=True, slots=True)
class ProjectionIdentity:
    """Everything that decides whether a projection can be reused as-is.

    ``built_at`` is deliberately absent: it records when the bytes were produced and
    differs between two builds of the same inputs, so including it would force a
    rebuild on every run and make the reuse contract meaningless.
    """

    projection_contract_version: int
    projection_fingerprint: str
    source_release_id: str
    source_release_manifest_sha256: str
    producer_git_commit: str
    data_as_of: date
    datasets: tuple[ProjectionDataset, ...]
    objects: tuple[ProjectionObject, ...]


@dataclass(frozen=True, slots=True)
class ProjectionBuildReport:
    path: Path
    identity: ProjectionIdentity
    built_at: datetime
    reused: bool
    rows: Mapping[str, int]
    transfers: TransferAccounting


def projection_fingerprint(datasets: Sequence[LakeDataset]) -> str:
    """Hash the shape this projection materializes, not the data it holds."""

    contract = {
        "projection_contract_version": PROJECTION_CONTRACT_VERSION,
        "datasets": [
            {
                "name": dataset.name,
                "contract_version": dataset.contract_version,
                "table": dataset.sqlite_table,
                "columns": [
                    {
                        "name": column.name,
                        "type": column.sqlite_type,
                        "nullable": column.nullable,
                        "primary_key_ordinal": column.primary_key_ordinal,
                    }
                    for column in dataset.columns
                ],
                "indexes": [
                    {"name": index.name, "columns": list(index.columns)}
                    for index in dataset.projection_indexes
                ],
            }
            for dataset in sorted(datasets, key=lambda item: item.name)
        ],
    }
    payload = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def plan_identity(
    release: FixedRelease,
    *,
    dataset_names: Sequence[str],
    producer_git_commit: str,
) -> tuple[ProjectionIdentity, dict[str, tuple[PartitionManifest, ...]]]:
    """Resolve the exact objects a build would read, before reading any of them."""

    if not dataset_names:
        raise ProjectionError("a projection must contain at least one dataset")
    if len(set(dataset_names)) != len(dataset_names):
        raise ProjectionError("a projection cannot list the same dataset twice")
    datasets = [accepted_dataset(release, name) for name in sorted(dataset_names)]
    partitions: dict[str, tuple[PartitionManifest, ...]] = {}
    entries: list[ProjectionDataset] = []
    objects: list[ProjectionObject] = []
    for dataset in datasets:
        selected = selected_partitions(release, dataset.name)
        if not selected:
            raise ProjectionError(f"release publishes no partition for {dataset.name}")
        partitions[dataset.name] = selected
        manifest = release.dataset_manifest(dataset.name)
        entries.append(
            ProjectionDataset(
                dataset=dataset.name,
                build_id=manifest.build_id,
                contract_version=manifest.contract_version,
                manifest_sha256=release.dataset_manifest_sha256[dataset.name],
            )
        )
        objects.extend(
            ProjectionObject(
                dataset=dataset.name,
                object_key=item.key,
                sha256=item.sha256,
                bytes=item.bytes,
                rows=item.rows,
            )
            for item in partition_objects(selected)
        )
    identity = ProjectionIdentity(
        projection_contract_version=PROJECTION_CONTRACT_VERSION,
        projection_fingerprint=projection_fingerprint(datasets),
        source_release_id=release.release_id,
        source_release_manifest_sha256=release.manifest_sha256,
        producer_git_commit=producer_git_commit,
        data_as_of=release.data_as_of,
        datasets=tuple(sorted(entries, key=lambda item: item.dataset)),
        objects=tuple(sorted(objects, key=lambda item: (item.dataset, item.object_key))),
    )
    return identity, partitions


def build_projection(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    destination: Path,
    dataset_names: Sequence[str],
    producer_git_commit: str,
    force: bool = False,
    built_at: datetime | None = None,
) -> ProjectionBuildReport:
    """Reuse an identical projection, or rebuild one atomically from the release."""

    identity, partitions = plan_identity(
        release,
        dataset_names=dataset_names,
        producer_git_commit=producer_git_commit,
    )
    _require_replaceable(destination)
    expected_rows = _expected_rows(identity)
    if not force:
        existing = read_projection_identity(destination)
        if (
            existing is not None
            and existing == identity
            and _stored_rows(destination) == expected_rows
        ):
            return ProjectionBuildReport(
                path=destination,
                identity=identity,
                built_at=_read_built_at(destination),
                reused=True,
                rows=expected_rows,
                transfers=cache.transfers,
            )

    now = (built_at or datetime.now(UTC)).astimezone(UTC)
    before = cache.transfers.as_dict()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.building"
    )
    rows: dict[str, int] = {}
    try:
        connection = sqlite3.connect(temporary)
        try:
            connection.execute("PRAGMA journal_mode=OFF")
            connection.executescript(_META_SCHEMA)
            for name in sorted(partitions):
                dataset = accepted_dataset(release, name)
                connection.executescript(_table_schema(dataset))
                rows[name] = _load_dataset(
                    connection,
                    session=session,
                    cache=cache,
                    dataset=dataset,
                    partitions=partitions[name],
                )
            _write_identity(connection, identity=identity, built_at=now)
            connection.commit()
        finally:
            connection.close()
        _require_row_totals(rows, identity)
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return ProjectionBuildReport(
        path=destination,
        identity=identity,
        built_at=now,
        reused=False,
        rows=rows,
        transfers=cache.transfers.since(before),
    )


def read_projection_identity(path: Path) -> ProjectionIdentity | None:
    """The identity a projection claims, or ``None`` when it cannot claim one.

    Absent, partial, and corrupt all resolve to ``None`` so the caller rebuilds
    instead of having to tell those cases apart.
    """

    if not path.is_file():
        return None
    try:
        with _open_read_only(path) as connection:
            meta = connection.execute(
                "SELECT projection_contract_version, projection_fingerprint, source_release_id, "
                "source_release_manifest_sha256, producer_git_commit, data_as_of "
                "FROM projection_meta WHERE single_row = 1"
            ).fetchone()
            if meta is None:
                return None
            datasets = tuple(
                ProjectionDataset(str(row[0]), str(row[1]), int(row[2]), str(row[3]))
                for row in connection.execute(
                    "SELECT dataset, build_id, contract_version, manifest_sha256 "
                    "FROM projection_dataset ORDER BY dataset"
                )
            )
            objects = tuple(
                ProjectionObject(str(row[0]), str(row[1]), str(row[2]), int(row[3]), int(row[4]))
                for row in connection.execute(
                    "SELECT dataset, object_key, sha256, bytes, rows "
                    "FROM projection_object ORDER BY dataset, object_key"
                )
            )
            return ProjectionIdentity(
                projection_contract_version=int(meta[0]),
                projection_fingerprint=str(meta[1]),
                source_release_id=str(meta[2]),
                source_release_manifest_sha256=str(meta[3]),
                producer_git_commit=str(meta[4]),
                data_as_of=date.fromisoformat(str(meta[5])),
                datasets=datasets,
                objects=objects,
            )
    except (sqlite3.Error, ValueError, TypeError):
        return None


def _read_built_at(path: Path) -> datetime:
    with _open_read_only(path) as connection:
        row = connection.execute(
            "SELECT built_at FROM projection_meta WHERE single_row = 1"
        ).fetchone()
    return datetime.fromisoformat(str(row[0]))


def _expected_rows(identity: ProjectionIdentity) -> dict[str, int]:
    expected: dict[str, int] = {}
    for item in identity.objects:
        expected[item.dataset] = expected.get(item.dataset, 0) + item.rows
    return dict(sorted(expected.items()))


def _stored_rows(path: Path) -> dict[str, int] | None:
    """Count what the projection actually holds, or ``None`` when it cannot be counted.

    Metadata alone cannot prove a projection is intact: rows can be removed from a
    local file without touching the identity tables, and a reuse decided on metadata
    would then hand a consumer a silently short input.
    """

    try:
        with _open_read_only(path) as connection:
            names = [
                str(row[0])
                for row in connection.execute(
                    "SELECT dataset FROM projection_dataset ORDER BY dataset"
                )
            ]
            counts: dict[str, int] = {}
            for name in names:
                dataset = PILOT_DATASETS.get(name)
                if dataset is None:
                    return None
                row = connection.execute(
                    f"SELECT COUNT(*) FROM {dataset.sqlite_table}"  # nosec B608
                ).fetchone()
                counts[name] = int(row[0])
    except (sqlite3.Error, TypeError, ValueError):
        return None
    return dict(sorted(counts.items()))


def _require_replaceable(destination: Path) -> None:
    """Replace only what is absent or is already a projection.

    A build replaces its destination outright, so a mistyped path destroys whatever
    is there. The rule is stated positively on purpose: everything that is neither
    an empty path nor a projection is refused, files that are not SQLite included.
    Deciding by "it did not open as a database, so there is nothing to protect"
    would let a YAML view, a Parquet object, or a dotfile be overwritten in silence.
    """

    if not destination.exists():
        return
    refusal = ProjectionError(f"refusing to replace a file that is not a projection: {destination}")
    if not destination.is_file():
        raise refusal
    try:
        with _open_read_only(destination) as connection:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
    except sqlite3.Error as exc:
        raise refusal from exc
    if "projection_meta" not in tables:
        raise refusal


def _open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _table_schema(dataset: LakeDataset) -> str:
    columns = [
        f"  {column.name} {column.sqlite_type}{'' if column.nullable else ' NOT NULL'}"
        for column in dataset.columns
    ]
    primary_key = ", ".join(dataset.primary_key)
    body = ",\n".join([*columns, f"  PRIMARY KEY ({primary_key})"])
    statements = [f"CREATE TABLE {dataset.sqlite_table}(\n{body}\n);"]
    statements.extend(
        f"CREATE INDEX {index.name} ON {dataset.sqlite_table}({', '.join(index.columns)});"
        for index in dataset.projection_indexes
    )
    return "\n".join(statements)


def _load_dataset(
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


def _require_row_totals(rows: Mapping[str, int], identity: ProjectionIdentity) -> None:
    expected = _expected_rows(identity)
    for name, count in sorted(rows.items()):
        if count != expected.get(name, 0):
            raise LakeReadError(
                f"projection loaded {count} row(s) for {name}; "
                f"the release manifest published {expected.get(name, 0)}"
            )


def _write_identity(
    connection: sqlite3.Connection, *, identity: ProjectionIdentity, built_at: datetime
) -> None:
    connection.execute(
        "INSERT INTO projection_meta(single_row, projection_contract_version, "
        "projection_fingerprint, source_release_id, source_release_manifest_sha256, "
        "producer_git_commit, built_at, data_as_of) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
        (
            identity.projection_contract_version,
            identity.projection_fingerprint,
            identity.source_release_id,
            identity.source_release_manifest_sha256,
            identity.producer_git_commit,
            built_at.isoformat(),
            identity.data_as_of.isoformat(),
        ),
    )
    connection.executemany(
        "INSERT INTO projection_dataset(dataset, build_id, contract_version, manifest_sha256) "
        "VALUES (?, ?, ?, ?)",
        [
            (item.dataset, item.build_id, item.contract_version, item.manifest_sha256)
            for item in identity.datasets
        ],
    )
    connection.executemany(
        "INSERT INTO projection_object(dataset, object_key, sha256, bytes, rows) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (item.dataset, item.object_key, item.sha256, item.bytes, item.rows)
            for item in identity.objects
        ],
    )
