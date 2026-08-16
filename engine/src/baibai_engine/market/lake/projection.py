"""A disposable SQLite projection of one fixed release.

The projection is not an authority. It exists so the operational path keeps the
point-lookup shape SQLite is good at while the release stays the thing that is
published, and it can be deleted at any time because everything in it is derived
from immutable objects.

Reuse is decided by exact identity, never by age or by a version counter: the
release, the release manifest digest, every dataset manifest digest, every object
digest, and the projection contract fingerprint must all match. Anything else — a
differing field, a missing metadata table, an interrupted build — rebuilds. The
building commit is recorded beside the projection as audit and is deliberately not
part of that identity, because a commit moves for documentation or for the web app
without moving a byte of the projection. A build writes to a unique temporary file
and is promoted with a single rename, so a reader never observes a half-built
projection and a failed build leaves the previous one intact.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb

from .datasets import LAKE_DATASETS, LakeDataset
from .duck import LakeSession
from .models import PartitionManifest
from .objects import LakeObjectCache, TransferAccounting, sha256_file
from .reader import (
    FixedRelease,
    LakeReadError,
    accepted_dataset,
    iter_partition_rows,
    materialize_partitions,
    partition_objects,
    selected_partitions,
)
from .retention import exclusive_lock

PROJECTION_CONTRACT_VERSION = 2

_META_SCHEMA = """
CREATE TABLE projection_meta(
  single_row INTEGER NOT NULL PRIMARY KEY CHECK (single_row = 1),
  projection_contract_version INTEGER NOT NULL,
  projection_fingerprint TEXT NOT NULL,
  source_release_id TEXT NOT NULL,
  source_release_manifest_sha256 TEXT NOT NULL,
  builder_git_commit TEXT NOT NULL,
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

CREATE TABLE projection_integrity(
  dataset TEXT NOT NULL PRIMARY KEY,
  content_sha256 TEXT NOT NULL
);
"""

_MIN_FREE_BYTES = 64 * 1024 * 1024
_PROJECTION_EXPANSION_FACTOR = 5


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

    The building commit is absent for the same reason. It is recorded beside the
    projection as audit, but a repository commit changes for documentation, for the
    web app, for anything at all, and none of that moves a byte of a projection whose
    release, objects, and contract are unchanged. What does move those bytes — the
    published objects and the code that maps them into tables — is inside
    ``projection_fingerprint``.
    """

    projection_contract_version: int
    projection_fingerprint: str
    source_release_id: str
    source_release_manifest_sha256: str
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
    """Hash the shape this projection materializes and everything that materializes it.

    The runtimes are part of "everything". A projection is Parquet decoded by DuckDB and
    written through Python's SQLite, so a bug fix in either — a corrected type
    conversion, a changed coercion — changes the values a rebuild would produce while
    this repository's own code and the release manifest stay byte-identical. Without
    them in the identity, the upgrade that fixes the values is exactly the event that
    leaves the old ones in place.
    """

    contract = {
        "projection_contract_version": PROJECTION_CONTRACT_VERSION,
        "implementation_sha256": {
            name: sha256_file(Path(__file__).with_name(name))
            for name in ("datasets.py", "projection.py", "reader.py")
        },
        "runtime": {
            "duckdb": duckdb.__version__,
            "sqlite": sqlite3.sqlite_version,
        },
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
    builder_git_commit: str,
    still_current: Callable[[], tuple[str, str]] | None = None,
    force: bool = False,
    built_at: datetime | None = None,
) -> ProjectionBuildReport:
    """Reuse an identical projection, or rebuild one atomically from the release.

    Two writers are kept apart by two different mechanisms, because serializing them is
    not enough. The lock gives one destination one writer at a time, so two builds
    cannot interleave their replaces. ``still_current`` covers what the lock cannot: a
    build resolves the pointer before it queues, so the release it holds may have been
    superseded while it waited, and finishing last would publish a generation the
    pointer has already moved past. A caller that asked for "the current release"
    supplies it and the build refuses rather than going backwards; a caller that asked
    for a named or previous release means what it said, and supplies nothing.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(
        destination.with_name(f".{destination.name}.lock"), subject="projection destination"
    ):
        return _build_projection(
            session,
            release=release,
            cache=cache,
            destination=destination,
            dataset_names=dataset_names,
            builder_git_commit=builder_git_commit,
            still_current=still_current,
            force=force,
            built_at=built_at,
        )


def _build_projection(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    destination: Path,
    dataset_names: Sequence[str],
    builder_git_commit: str,
    still_current: Callable[[], tuple[str, str]] | None,
    force: bool,
    built_at: datetime | None,
) -> ProjectionBuildReport:
    identity, partitions = plan_identity(release, dataset_names=dataset_names)
    require_still_current(still_current, release)
    _require_replaceable(destination)
    expected_rows = expected_row_totals(identity)
    if not force:
        existing = read_projection_identity(destination)
        if (
            existing is not None
            and existing == identity
            and _projection_is_intact(destination, identity, expected_rows)
        ):
            # The integrity scan reads the whole projection — 47 seconds on the
            # production store — so current can move under it exactly as it can under a
            # rebuild. Reporting a reuse without asking again would answer "this is the
            # current projection" about a release that stopped being current while the
            # answer was being computed.
            require_still_current(still_current, release)
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
    require_durable_filesystem(destination.parent)
    require_free_capacity(destination.parent, identity)
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
                rows[name] = load_dataset_rows(
                    connection,
                    session=session,
                    cache=cache,
                    dataset=dataset,
                    partitions=partitions[name],
                )
                connection.executescript(_index_schema(dataset))
                connection.execute(f"ANALYZE {dataset.sqlite_table}")  # nosec B608
            integrity = {
                name: _table_content_sha256(connection, accepted_dataset(release, name))
                for name in sorted(partitions)
            }
            _write_identity(
                connection,
                identity=identity,
                builder_git_commit=builder_git_commit,
                built_at=now,
                integrity=integrity,
            )
            connection.commit()
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ProjectionError("projection SQLite quick_check failed")
        finally:
            connection.close()
        _require_row_totals(rows, identity)
        require_still_current(still_current, release)
        durable_replace(temporary, destination)
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


def require_still_current(
    still_current: Callable[[], tuple[str, str]] | None, release: FixedRelease
) -> None:
    """The full identity, not the name: an ID can be reused for different bytes.

    A recovery tool that republishes a manifest under an existing release ID would pass
    a name comparison while pointing at a different graph, and the projection built from
    the old bytes would take the current destination.
    """

    if still_current is None:
        return
    actual = still_current()
    expected = (release.release_id, release.manifest_sha256)
    if actual != expected:
        raise ProjectionError(
            f"the current release moved while this projection was building: "
            f"{expected[0]} is no longer current ({actual[0]} is)"
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
                "source_release_manifest_sha256, data_as_of "
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
                data_as_of=date.fromisoformat(str(meta[4])),
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


def expected_row_totals(identity: ProjectionIdentity) -> dict[str, int]:
    expected: dict[str, int] = {}
    for item in identity.objects:
        expected[item.dataset] = expected.get(item.dataset, 0) + item.rows
    return dict(sorted(expected.items()))


def _projection_is_intact(
    path: Path,
    identity: ProjectionIdentity,
    expected_rows: Mapping[str, int],
) -> bool:
    """Verify row values, table schema, and secondary indexes before reuse."""
    try:
        with _open_read_only(path) as connection:
            stored_integrity = {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    "SELECT dataset, content_sha256 FROM projection_integrity ORDER BY dataset"
                )
            }
            names = tuple(item.dataset for item in identity.datasets)
            if set(stored_integrity) != set(names):
                return False
            for name in names:
                dataset = LAKE_DATASETS.get(name)
                if dataset is None:
                    return False
                count = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {dataset.sqlite_table}"  # nosec B608
                    ).fetchone()[0]
                )
                if count != expected_rows.get(name, 0):
                    return False
                if _actual_table_shape(connection, dataset) != _expected_table_shape(dataset):
                    return False
                if _actual_indexes(connection, dataset) != _expected_indexes(dataset):
                    return False
                if _table_content_sha256(connection, dataset) != stored_integrity[name]:
                    return False
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            return bool(quick_check == ("ok",))
    except (sqlite3.Error, TypeError, ValueError):
        return False


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


def require_free_capacity(parent: Path, identity: ProjectionIdentity) -> None:
    published_bytes = sum(item.bytes for item in identity.objects)
    required = max(_MIN_FREE_BYTES, published_bytes * _PROJECTION_EXPANSION_FACTOR)
    if shutil.disk_usage(parent).free < required:
        raise ProjectionError(
            f"projection build requires at least {required} free bytes in {parent}"
        )


def _expected_table_shape(dataset: LakeDataset) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            ordinal,
            column.name,
            column.sqlite_type,
            0 if column.nullable else 1,
            None,
            column.primary_key_ordinal,
        )
        for ordinal, column in enumerate(dataset.columns)
    )


def _actual_table_shape(
    connection: sqlite3.Connection, dataset: LakeDataset
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in connection.execute(f"PRAGMA table_info({dataset.sqlite_table})")  # nosec B608
    )


def _expected_indexes(dataset: LakeDataset) -> tuple[tuple[object, ...], ...]:
    column_ids = {column.name: index for index, column in enumerate(dataset.columns)}
    indexes: list[tuple[object, ...]] = []
    for item in dataset.projection_indexes:
        details: list[tuple[object, ...]] = [
            (sequence, column_ids[name], name, 0, "BINARY", 1)
            for sequence, name in enumerate(item.columns)
        ]
        details.append((len(item.columns), -1, None, 0, "BINARY", 0))
        indexes.append((item.name, 0, "c", 0, tuple(details)))
    return tuple(sorted(indexes))


def _actual_indexes(
    connection: sqlite3.Connection, dataset: LakeDataset
) -> tuple[tuple[object, ...], ...]:
    indexes: list[tuple[object, ...]] = []
    for row in connection.execute(f"PRAGMA index_list({dataset.sqlite_table})"):  # nosec B608
        name = str(row[1])
        if str(row[3]) == "pk":
            continue
        details = tuple(
            tuple(item)
            for item in connection.execute(f"PRAGMA index_xinfo({name})")  # nosec B608
        )
        indexes.append((name, int(row[2]), str(row[3]), int(row[4]), details))
    return tuple(sorted(indexes))


def require_durable_filesystem(parent: Path) -> None:
    """Fail before the expensive load unless publication primitives are supported."""
    token = uuid.uuid4().hex
    upper = parent / f".projection-{token}-Case.probe"
    lower = parent / f".projection-{token}-case.probe"
    linked = parent / f".projection-{token}-linked.probe"
    renamed = parent / f".projection-{token}-renamed.probe"
    directory = -1
    try:
        for path in (upper, lower):
            with path.open("xb") as probe:
                probe.write(b"projection-filesystem-probe")
                probe.flush()
                os.fsync(probe.fileno())
        if upper.samefile(lower):
            raise ProjectionError("projection requires a case-sensitive filesystem")
        linked.hardlink_to(lower)
        if not linked.samefile(lower):
            raise ProjectionError("projection filesystem does not preserve hard-link identity")
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        os.fsync(directory)
        upper.replace(renamed)
        os.fsync(directory)
    except ProjectionError:
        raise
    except OSError as exc:
        raise ProjectionError(
            "projection filesystem does not support durable same-directory publication"
        ) from exc
    finally:
        if directory >= 0:
            os.close(directory)
        for path in (upper, lower, linked, renamed):
            with suppress(OSError):
                path.unlink(missing_ok=True)


def _table_content_sha256(connection: sqlite3.Connection, dataset: LakeDataset) -> str:
    names = tuple(column.name for column in dataset.columns)
    columns = ", ".join(names)
    order = ", ".join(dataset.primary_key)
    cursor = connection.execute(
        f"SELECT {columns} FROM {dataset.sqlite_table} ORDER BY {order}"  # nosec B608
    )
    digest = hashlib.sha256()
    while rows := cursor.fetchmany(20_000):
        for row in rows:
            payload = json.dumps(
                row,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
    return digest.hexdigest()


def durable_replace(temporary: Path, destination: Path) -> None:
    with temporary.open("rb") as source:
        os.fsync(source.fileno())
    rollback = destination.with_name(
        f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.rollback"
    )
    try:
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise ProjectionError(
            "projection filesystem does not support directory durability"
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
                raise ProjectionError(
                    "projection publication durability failed and rollback could not be proven"
                ) from recovery_error
            raise ProjectionError(
                "projection publication durability failed; the previous generation was restored"
            ) from exc
        with suppress(OSError):
            rollback.unlink(missing_ok=True)
            # The new destination is already durable. A retained hidden hard link
            # does not change the projection path or its contents.
    finally:
        os.close(directory)


def _table_schema(dataset: LakeDataset) -> str:
    columns = [
        f"  {column.name} {column.sqlite_type}{'' if column.nullable else ' NOT NULL'}"
        for column in dataset.columns
    ]
    primary_key = ", ".join(dataset.primary_key)
    body = ",\n".join([*columns, f"  PRIMARY KEY ({primary_key})"])
    return f"CREATE TABLE {dataset.sqlite_table}(\n{body}\n);"


def _index_schema(dataset: LakeDataset) -> str:
    return "\n".join(
        f"CREATE INDEX {index.name} ON {dataset.sqlite_table}({', '.join(index.columns)});"
        for index in dataset.projection_indexes
    )


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


def _require_row_totals(rows: Mapping[str, int], identity: ProjectionIdentity) -> None:
    expected = expected_row_totals(identity)
    for name, count in sorted(rows.items()):
        if count != expected.get(name, 0):
            raise LakeReadError(
                f"projection loaded {count} row(s) for {name}; "
                f"the release manifest published {expected.get(name, 0)}"
            )


def _write_identity(
    connection: sqlite3.Connection,
    *,
    identity: ProjectionIdentity,
    builder_git_commit: str,
    built_at: datetime,
    integrity: Mapping[str, str],
) -> None:
    connection.execute(
        "INSERT INTO projection_meta(single_row, projection_contract_version, "
        "projection_fingerprint, source_release_id, source_release_manifest_sha256, "
        "builder_git_commit, built_at, data_as_of) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
        (
            identity.projection_contract_version,
            identity.projection_fingerprint,
            identity.source_release_id,
            identity.source_release_manifest_sha256,
            builder_git_commit,
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
        "INSERT INTO projection_integrity(dataset, content_sha256) VALUES (?, ?)",
        sorted(integrity.items()),
    )
    connection.executemany(
        "INSERT INTO projection_object(dataset, object_key, sha256, bytes, rows) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (item.dataset, item.object_key, item.sha256, item.bytes, item.rows)
            for item in identity.objects
        ],
    )
