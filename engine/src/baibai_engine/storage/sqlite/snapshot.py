"""SQLite snapshotの作成・整合検査と転送用のCLI。"""

from __future__ import annotations

import argparse
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


def backup_to_new_file(source: Path, output: Path) -> int:
    """Use SQLite backup so committed WAL pages belong to the captured generation."""
    if not source.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"SQLite snapshot output already exists: {output}")
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    try:
        with (
            closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source_db,
            closing(sqlite3.connect(output, timeout=30)) as target_db,
            target_db,
        ):
            source_db.backup(target_db, pages=1024, sleep=0.05)
        return validate_snapshot(output)
    except Exception:
        output.unlink(missing_ok=True)
        raise


def validate_snapshot(path: Path) -> int:
    """Return schema version after integrity validation of a sealed snapshot."""
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
        row = connection.execute("PRAGMA quick_check").fetchone()
        if row is None or row[0] != "ok":
            raise sqlite3.DatabaseError(f"SQLite quick_check failed: {path}")
        return _schema_version(connection, path)


def _schema_version(connection: sqlite3.Connection, path: Path) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    if row is None:
        raise sqlite3.DatabaseError(f"SQLite user_version is unavailable: {path}")
    return int(row[0])


def validate_database(path: Path, *, expected_schema_version: int | None = None) -> None:
    """Check integrity and any requested schema version against the same SQLite snapshot."""

    if not path.is_file():
        raise FileNotFoundError(f"SQLite file does not exist: {path}")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        if expected_schema_version is not None:
            connection.execute("BEGIN")
        result = connection.execute("PRAGMA quick_check").fetchone()
        if result is None or result[0] != "ok":
            raise sqlite3.DatabaseError(f"SQLite quick_check failed for {path}: {result}")
        if expected_schema_version is not None:
            actual = _schema_version(connection, path)
            if actual != expected_schema_version:
                raise sqlite3.DatabaseError(
                    f"SQLite schema version mismatch for {path}: "
                    f"expected={expected_schema_version} actual={actual}"
                )


def database_schema_version(path: Path) -> int:
    """Return ``PRAGMA user_version`` without opening the store for writes."""
    if not path.is_file():
        raise FileNotFoundError(f"SQLite file does not exist: {path}")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        return _schema_version(connection, path)


def _try_smaller_pages(source: Path, output: Path) -> bool:
    """Use one regular SQLite file when the unchanged source proves it is smaller."""
    with closing(
        sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    ) as db:
        version = db.execute("PRAGMA data_version").fetchone()
        # With no free pages the existing backup has exactly this page count.
        # Leave pruning and non-default layouts to the normal backup path.
        if db.execute("PRAGMA page_size").fetchone() != (4096,) or db.execute(
            "PRAGMA freelist_count"
        ).fetchone() != (0,):
            return False
        baseline_size = db.execute("PRAGMA page_count").fetchone()[0] * 4096
        wal = db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        # VACUUM rebuilds indexes: reject an already broken input rather than
        # allowing the rebuild to hide it. data_version fences the two reads.
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise sqlite3.DatabaseError(f"SQLite quick_check failed: {source}")
        with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=output.parent) as directory:
            packed = Path(directory) / "snapshot.sqlite"
            db.execute("PRAGMA page_size=1024")
            try:
                db.execute("VACUUM INTO ?", (str(packed),))
            except sqlite3.OperationalError as error:
                if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
                    return False
                raise
            if db.execute("PRAGMA data_version").fetchone() != version or (
                packed.stat().st_size >= baseline_size
            ):
                return False
            if wal:
                with closing(sqlite3.connect(packed)) as target:
                    if target.execute("PRAGMA journal_mode=WAL").fetchone() != ("wal",):
                        raise sqlite3.DatabaseError("could not retain snapshot journal mode")
            validate_snapshot(packed)
            packed.replace(output)
            return True


def create_snapshot(source: Path, output: Path, *, compact: bool = False) -> None:
    """Capture a standalone SQLite store, including committed WAL rows."""
    if source.resolve() == output.resolve() or (
        source.exists() and output.exists() and source.samefile(output)
    ):
        raise ValueError("SQLite source and output must not be the same file")
    output.parent.mkdir(parents=True, exist_ok=True)
    if compact and source.is_file() and _try_smaller_pages(source, output):
        return
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=output.parent) as directory:
        temporary = Path(directory) / "snapshot.sqlite"
        backup_to_new_file(source, temporary)
        if compact:
            # Only the transfer copy is disposable. Keep source pages and WAL untouched.
            with closing(sqlite3.connect(temporary)) as connection:
                free_pages = connection.execute("PRAGMA freelist_count").fetchone()[0]
                if free_pages:
                    connection.execute("VACUUM")
            if free_pages:
                validate_snapshot(temporary)
        temporary.replace(output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("create")
    snapshot.add_argument("--source", type=Path, required=True)
    snapshot.add_argument("--output", type=Path, required=True)
    snapshot.add_argument("--compact", action="store_true", help="pack the SQLite transfer copy")
    check = subparsers.add_parser("check")
    check.add_argument("--path", type=Path, required=True)
    check.add_argument("--schema-version", type=int)
    version = subparsers.add_parser("version")
    version.add_argument("--path", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "create":
        create_snapshot(args.source, args.output, compact=args.compact)
    elif args.command == "version":
        print(database_schema_version(args.path))
    else:
        validate_database(args.path, expected_schema_version=args.schema_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
