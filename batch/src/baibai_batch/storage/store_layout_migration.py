"""One-time, reversible migration between repository store layouts.

This module is an operational cutover surface, not a stable public CLI.  It keeps
the canonical database file itself intact: all writers must be stopped, SQLite
state is inspected read-only, and each resource is renamed atomically on one
filesystem.  A conflict is never merged or overwritten.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from urllib.parse import quote

from baibai_engine.batch_api import (
    APPLICATION_DB_PATH,
    APPLICATION_SCHEMA_VERSION,
    CALIBRATION_DIR,
    MACRO_DB_PATH,
    MACRO_SCHEMA_VERSION,
    MARKET_DB_PATH,
    MARKET_SCHEMA_VERSION,
    RUN_STORE_SCHEMA_VERSION,
    RUNS_DB_PATH,
    STORE_LAYOUT_MAPPINGS,
)

Direction = Literal["forward", "rollback"]

_SQLITE_KINDS = {"application", "market", "macro", "runs"}
_KIND_BY_CURRENT_PATH = {
    APPLICATION_DB_PATH: "application",
    MARKET_DB_PATH: "market",
    MACRO_DB_PATH: "macro",
    RUNS_DB_PATH: "runs",
    CALIBRATION_DIR: "calibration",
}
_REQUIRED_TABLES = {
    "application": frozenset(
        {
            "bargain_assessment",
            "holding_review",
            "ledger_event",
            "ledger_market_price",
            "ledger_meta",
            "macro_context",
            "macro_context_head",
            "operation_session",
            "portfolio_outcome",
            "proposal",
            "shortlist",
            "task",
            "thesis",
            "thesis_review",
        }
    ),
    "market": frozenset(
        {
            "edinet_buyback_reports",
            "edinet_document_lists",
            "edinet_documents",
            "edinet_metrics",
            "jpx_regulation_flags",
            "jpx_regulation_sources",
            "jquants_daily_bars",
            "jquants_earnings_calendar",
            "jquants_fin_summaries",
            "jquants_market_calendar",
            "jquants_master_snapshots",
            "jquants_margin_alerts",
            "jquants_all_issues_daily_margin",
            "jquants_weekly_margin",
            "source_coverage",
        }
    ),
    "macro": frozenset(
        {
            "aliases",
            "observations",
            "provider_runs",
            "registry_prune_authorizations",
            "registry_state",
            "series",
        }
    ),
    "runs": frozenset(
        {
            "screening_candidate",
            "screening_run",
            "screening_selection",
            "selection_entry",
        }
    ),
}
_EXPECTED_SCHEMA_VERSIONS = {
    "application": APPLICATION_SCHEMA_VERSION,
    "market": MARKET_SCHEMA_VERSION,
    "macro": MACRO_SCHEMA_VERSION,
    "runs": RUN_STORE_SCHEMA_VERSION,
}
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    _RENAMEAT2.restype = ctypes.c_int


class StoreLayoutMigrationError(RuntimeError):
    """The cutover cannot continue without risking a split or data loss."""


@dataclass(frozen=True, slots=True)
class LayoutEntry:
    kind: str
    source: Path
    destination: Path
    status: Literal["pending", "complete", "missing"]


@dataclass(frozen=True, slots=True)
class SQLiteIdentity:
    device: int
    inode: int
    size: int
    schema_version: int
    tables: tuple[str, ...]
    application_row_counts: tuple[tuple[str, int], ...]
    ledger_append_head: int | None
    application_dump_sha256: str | None


@dataclass(frozen=True, slots=True)
class DirectoryIdentity:
    device: int
    inode: int
    entries: int
    total_bytes: int
    manifest_sha256: str


Identity = SQLiteIdentity | DirectoryIdentity


@dataclass(frozen=True, slots=True)
class MigrationResult:
    direction: Direction
    applied: bool
    backup: str | None
    resources: tuple[tuple[str, str], ...]
    verification: tuple[dict[str, object], ...]


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _entry_paths(root: Path, direction: Direction) -> tuple[LayoutEntry, ...]:
    entries: list[LayoutEntry] = []
    for legacy_relative, current_relative in STORE_LAYOUT_MAPPINGS:
        legacy = root / legacy_relative
        current = root / current_relative
        source, destination = (legacy, current) if direction == "forward" else (current, legacy)
        source_exists = _lexists(source)
        destination_exists = _lexists(destination)
        if source_exists and destination_exists:
            raise StoreLayoutMigrationError(
                f"store layout conflict: both {source.relative_to(root)} and "
                f"{destination.relative_to(root)} exist; no files were changed"
            )
        status: Literal["pending", "complete", "missing"]
        if source_exists:
            status = "pending"
        elif destination_exists:
            status = "complete"
        else:
            status = "missing"
        entries.append(
            LayoutEntry(
                kind=_KIND_BY_CURRENT_PATH[current_relative],
                source=source,
                destination=destination,
                status=status,
            )
        )
    application = next(entry for entry in entries if entry.kind == "application")
    if application.status == "missing":
        raise StoreLayoutMigrationError("canonical application DB is missing from both layouts")
    return tuple(entries)


def _sqlite_uri(path: Path) -> str:
    return f"file:{quote(str(path.resolve()))}?mode=ro&immutable=1"


def _sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal"))


def _validate_sqlite(path: Path, kind: str) -> SQLiteIdentity:
    if path.is_symlink() or not path.is_file():
        raise StoreLayoutMigrationError(f"{path} must be a regular SQLite file, not a symlink")
    present_sidecars = [sidecar for sidecar in _sidecars(path) if _lexists(sidecar)]
    if present_sidecars:
        rendered = ", ".join(str(sidecar) for sidecar in present_sidecars)
        raise StoreLayoutMigrationError(
            f"SQLite sidecar exists ({rendered}); stop every writer and checkpoint it first"
        )

    try:
        with sqlite3.connect(_sqlite_uri(path), uri=True) as connection:
            integrity = tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check"))
            if integrity != ("ok",):
                raise StoreLayoutMigrationError(f"{path} integrity_check failed: {integrity}")
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_keys:
                raise StoreLayoutMigrationError(
                    f"{path} foreign_key_check failed with {len(foreign_keys)} row(s)"
                )
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            expected_schema_version = _EXPECTED_SCHEMA_VERSIONS[kind]
            if schema_version != expected_schema_version:
                raise StoreLayoutMigrationError(
                    f"{path} schema version is {schema_version}; "
                    f"current {kind} code requires {expected_schema_version}"
                )
            tables = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            )
            missing = sorted(_REQUIRED_TABLES[kind].difference(tables))
            if missing:
                raise StoreLayoutMigrationError(
                    f"{path} is missing required tables: {', '.join(missing)}"
                )

            row_counts: tuple[tuple[str, int], ...] = ()
            ledger_head: int | None = None
            dump_sha256: str | None = None
            if kind == "application":
                row_count_values: list[tuple[str, int]] = []
                for table in tables:
                    # Names come from this database's sqlite_master and are quoted identifiers.
                    query = f'SELECT count(*) FROM "{table}"'  # nosec B608
                    row_count_values.append((table, int(connection.execute(query).fetchone()[0])))
                row_counts = tuple(row_count_values)
                ledger_head = int(
                    connection.execute(
                        "SELECT coalesce(max(append_seq), 0) FROM ledger_event"
                    ).fetchone()[0]
                )
                digest = hashlib.sha256()
                for statement in connection.iterdump():
                    digest.update(statement.encode("utf-8"))
                    digest.update(b"\n")
                dump_sha256 = digest.hexdigest()
    except sqlite3.DatabaseError as error:
        raise StoreLayoutMigrationError(f"cannot validate SQLite store {path}: {error}") from error

    stat = path.stat()
    return SQLiteIdentity(
        device=stat.st_dev,
        inode=stat.st_ino,
        size=stat.st_size,
        schema_version=schema_version,
        tables=tables,
        application_row_counts=row_counts,
        ledger_append_head=ledger_head,
        application_dump_sha256=dump_sha256,
    )


def _validate_directory(path: Path) -> DirectoryIdentity:
    if path.is_symlink() or not path.is_dir():
        raise StoreLayoutMigrationError(f"{path} must be a directory, not a symlink")
    digest = hashlib.sha256()
    entries = 0
    total_bytes = 0
    for child in sorted(path.rglob("*")):
        if child.is_symlink():
            raise StoreLayoutMigrationError(f"calibration tree contains a symlink: {child}")
        relative = child.relative_to(path).as_posix()
        stat = child.stat()
        kind = "d" if child.is_dir() else "f"
        size = 0 if child.is_dir() else stat.st_size
        digest.update(
            f"{kind}\0{relative}\0{stat.st_dev}\0{stat.st_ino}\0{size}\0{stat.st_mtime_ns}\n".encode()
        )
        entries += 1
        total_bytes += size
    root_stat = path.stat()
    return DirectoryIdentity(
        device=root_stat.st_dev,
        inode=root_stat.st_ino,
        entries=entries,
        total_bytes=total_bytes,
        manifest_sha256=digest.hexdigest(),
    )


def _validate(entry: LayoutEntry, path: Path) -> Identity:
    if entry.kind in _SQLITE_KINDS:
        return _validate_sqlite(path, entry.kind)
    return _validate_directory(path)


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path.parent
    while not candidate.exists():
        if candidate == candidate.parent:
            raise StoreLayoutMigrationError(f"cannot resolve destination filesystem for {path}")
        candidate = candidate.parent
    return candidate


def _preflight(
    root: Path, direction: Direction
) -> tuple[tuple[LayoutEntry, ...], dict[str, Identity]]:
    entries = _entry_paths(root, direction)
    layout_sidecars = [
        sidecar
        for entry in entries
        if entry.kind in _SQLITE_KINDS
        for path in (entry.source, entry.destination)
        for sidecar in _sidecars(path)
        if _lexists(sidecar)
    ]
    if layout_sidecars:
        rendered = ", ".join(str(sidecar) for sidecar in layout_sidecars)
        raise StoreLayoutMigrationError(
            f"SQLite sidecar exists ({rendered}); stop every writer and checkpoint it first"
        )
    identities: dict[str, Identity] = {}
    for entry in entries:
        if entry.status == "missing":
            continue
        path = entry.source if entry.status == "pending" else entry.destination
        identities[entry.kind] = _validate(entry, path)
        if entry.status == "pending":
            source_device = path.stat().st_dev
            destination_device = _nearest_existing_parent(entry.destination).stat().st_dev
            if source_device != destination_device:
                raise StoreLayoutMigrationError(
                    f"{entry.source} -> {entry.destination} crosses filesystems; "
                    "atomic rename is unavailable"
                )
    return entries, identities


def _logical_sqlite_identity(identity: SQLiteIdentity) -> tuple[object, ...]:
    return (
        identity.schema_version,
        identity.tables,
        identity.application_row_counts,
        identity.ledger_append_head,
        identity.application_dump_sha256,
    )


def _backup_application(root: Path, entry: LayoutEntry, identity: SQLiteIdentity) -> Path:
    source = entry.source if entry.status == "pending" else entry.destination
    backup_dir = root / APPLICATION_DB_PATH.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_dir / f"baibai-layout-{timestamp}.sqlite"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".store-layout-", suffix=".sqlite", dir=backup_dir
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with (
            sqlite3.connect(_sqlite_uri(source), uri=True) as source_connection,
            sqlite3.connect(temporary) as backup_connection,
        ):
            source_connection.backup(backup_connection)
        backup_identity = _validate_sqlite(temporary, "application")
        if _logical_sqlite_identity(backup_identity) != _logical_sqlite_identity(identity):
            raise StoreLayoutMigrationError("application backup identity differs from canonical DB")
        temporary.replace(backup)
    finally:
        temporary.unlink(missing_ok=True)
    return backup


def _atomic_move(source: Path, destination: Path) -> None:
    if _lexists(destination):
        raise StoreLayoutMigrationError(f"destination appeared during migration: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "linux" or _RENAMEAT2 is None:
        raise StoreLayoutMigrationError(
            "this platform has no verified atomic no-replace rename; no files were changed"
        )
    ctypes.set_errno(0)
    result = _RENAMEAT2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise StoreLayoutMigrationError(f"destination appeared during migration: {destination}")
    raise StoreLayoutMigrationError(
        f"atomic rename failed for {source} -> {destination}: "
        f"[{error_number}] {os.strerror(error_number)}"
    )


def _verification(identities: dict[str, Identity]) -> tuple[dict[str, object], ...]:
    result: list[dict[str, object]] = []
    for kind in _KIND_BY_CURRENT_PATH.values():
        identity = identities.get(kind)
        if isinstance(identity, SQLiteIdentity):
            result.append(
                {
                    "kind": kind,
                    "schema_version": identity.schema_version,
                    "size_bytes": identity.size,
                    "table_count": len(identity.tables),
                    "application_row_counts": dict(identity.application_row_counts),
                    "ledger_append_head": identity.ledger_append_head,
                    "application_dump_sha256": identity.application_dump_sha256,
                }
            )
        elif isinstance(identity, DirectoryIdentity):
            result.append(
                {
                    "kind": kind,
                    "entries": identity.entries,
                    "total_bytes": identity.total_bytes,
                    "manifest_sha256": identity.manifest_sha256,
                }
            )
    return tuple(result)


def _remove_empty_legacy_directories(root: Path) -> None:
    legacy_root = root / STORE_LAYOUT_MAPPINGS[0][0].parts[0]
    candidates: set[Path] = set()
    for legacy, _current in STORE_LAYOUT_MAPPINGS:
        candidate = (root / legacy).parent
        while candidate != root:
            candidates.add(candidate)
            if candidate == legacy_root:
                break
            candidate = candidate.parent
    for candidate in sorted(candidates, key=lambda path: len(path.parts), reverse=True):
        with suppress(OSError):
            candidate.rmdir()


def migrate(root: Path, direction: Direction, *, apply: bool = False) -> MigrationResult:
    """Validate a layout and optionally rename every pending resource.

    The default is a no-write dry run.  On any move or post-move verification
    failure, completed renames are reversed before the error is returned.
    """

    root = root.resolve()
    entries, before = _preflight(root, direction)
    pending = tuple(entry for entry in entries if entry.status == "pending")
    resources = tuple((entry.kind, entry.status) for entry in entries)
    if not apply or not pending:
        return MigrationResult(direction, False, None, resources, _verification(before))

    application_entry = next(entry for entry in entries if entry.kind == "application")
    application_identity = cast(SQLiteIdentity, before["application"])
    backup = _backup_application(root, application_entry, application_identity)
    moved: list[LayoutEntry] = []
    try:
        for entry in pending:
            _atomic_move(entry.source, entry.destination)
            moved.append(entry)
        after_entries, after = _preflight(root, direction)
        if any(entry.status == "pending" for entry in after_entries):
            raise StoreLayoutMigrationError("migration left pending resources")
        for entry in moved:
            if after[entry.kind] != before[entry.kind]:
                raise StoreLayoutMigrationError(
                    f"{entry.kind} identity changed during atomic rename"
                )
    except Exception as error:
        recovery_errors: list[str] = []
        for entry in reversed(moved):
            try:
                _atomic_move(entry.destination, entry.source)
            except Exception as recovery_error:  # pragma: no cover - catastrophic filesystem fault
                recovery_errors.append(f"{entry.kind}: {recovery_error}")
        for entry in moved:
            try:
                if _validate(entry, entry.source) != before[entry.kind]:
                    recovery_errors.append(f"{entry.kind}: recovered identity differs")
            except Exception as recovery_error:  # pragma: no cover - catastrophic filesystem fault
                recovery_errors.append(f"{entry.kind}: cannot validate recovery: {recovery_error}")
        if recovery_errors:
            raise StoreLayoutMigrationError(
                f"migration failed ({error}); automatic rollback also failed: "
                + "; ".join(recovery_errors)
            ) from error
        if isinstance(error, StoreLayoutMigrationError):
            raise
        raise StoreLayoutMigrationError(f"migration failed and was rolled back: {error}") from error

    if direction == "forward":
        _remove_empty_legacy_directories(root)
    final_resources = tuple((entry.kind, entry.status) for entry in after_entries)
    return MigrationResult(
        direction,
        True,
        str(backup.relative_to(root)),
        final_resources,
        _verification(after),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("direction", choices=("forward", "rollback"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="validate only (the default)")
    mode.add_argument("--apply", action="store_true", help="create a backup and atomically rename")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = migrate(args.root, cast(Direction, args.direction), apply=bool(args.apply))
    except StoreLayoutMigrationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MigrationResult",
    "StoreLayoutMigrationError",
    "main",
    "migrate",
]
