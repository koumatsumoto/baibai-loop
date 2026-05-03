"""Integrity checks for the git-tracked raw JSON tree under
`records/_data/raw/screening/`.

Two failure modes the verifier guards against:

1. A file is committed that is at or above GitHub's 50MB warning threshold,
   risking the 100MB hard block on subsequent rewrites.
2. The SQLite cache (when present) drifts away from the raw JSON, e.g. a
   rebuild was skipped after a new fetch landed on disk. We compare each
   on-disk SHA-256 against `raw_imports.sha256` and surface mismatches.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MAX_FILE_SIZE_MB = 50


@dataclass(frozen=True, slots=True)
class FileSizeViolation:
    path: Path
    size: int


@dataclass(frozen=True, slots=True)
class SQLiteIntegrityIssue:
    path: Path
    expected_sha256: str | None
    actual_sha256: str
    reason: str


@dataclass(frozen=True, slots=True)
class VerifyResult:
    file_count: int
    total_bytes: int
    max_file_size: int
    size_violations: tuple[FileSizeViolation, ...] = field(default_factory=tuple)
    sqlite_issues: tuple[SQLiteIntegrityIssue, ...] = field(default_factory=tuple)

    @property
    def has_failures(self) -> bool:
        return bool(self.size_violations) or bool(self.sqlite_issues)


def verify_raw_cache(
    raw_dir: Path,
    *,
    max_size_mb: int = DEFAULT_MAX_FILE_SIZE_MB,
    sqlite_path: Path | None = None,
) -> VerifyResult:
    """Walk `raw_dir` for `*.json` files and report any violations.

    `manifests/` is excluded because its contents are run-output artefacts
    that are .gitignore'd; including them would couple verification to
    transient state. `sqlite_path` is optional — when omitted, only the size
    check runs.
    """
    if not raw_dir.exists():
        return VerifyResult(file_count=0, total_bytes=0, max_file_size=0)

    files = sorted(_iter_raw_json_files(raw_dir))
    file_count = len(files)
    total_bytes = sum(path.stat().st_size for path in files)
    max_file_size = max((path.stat().st_size for path in files), default=0)

    max_bytes = max_size_mb * 1024 * 1024
    size_violations = tuple(
        FileSizeViolation(path=path, size=path.stat().st_size)
        for path in files
        if path.stat().st_size >= max_bytes
    )

    sqlite_issues: tuple[SQLiteIntegrityIssue, ...] = ()
    if sqlite_path is not None and sqlite_path.exists():
        sqlite_issues = _check_sqlite_integrity(files, sqlite_path)

    return VerifyResult(
        file_count=file_count,
        total_bytes=total_bytes,
        max_file_size=max_file_size,
        size_violations=size_violations,
        sqlite_issues=sqlite_issues,
    )


def _iter_raw_json_files(raw_dir: Path) -> Iterable[Path]:
    for path in raw_dir.rglob("*.json"):
        if not path.is_file():
            continue
        # Skip per-run lineage manifests; they live under manifests/ and are
        # gitignored as derived run output (see automation-v1.md §11).
        if "manifests" in path.relative_to(raw_dir).parts:
            continue
        yield path


def _check_sqlite_integrity(
    files: Iterable[Path], sqlite_path: Path
) -> tuple[SQLiteIntegrityIssue, ...]:
    conn = sqlite3.connect(sqlite_path)
    try:
        try:
            conn.execute("SELECT 1 FROM raw_imports LIMIT 1")
        except sqlite3.OperationalError:
            # SQLite exists but predates the schema with raw_imports — not an
            # integrity failure, just nothing to cross-check.
            return ()
        recorded = {
            row[0]: row[1]
            for row in conn.execute("SELECT path, sha256 FROM raw_imports").fetchall()
        }
    finally:
        conn.close()

    issues: list[SQLiteIntegrityIssue] = []
    for path in files:
        actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        recorded_sha = recorded.get(path.as_posix())
        if recorded_sha is None:
            # Not every raw JSON file has to be imported (e.g. earnings cal /
            # market cal are still JSON-only). Treat as informational only;
            # rebuild_from_raw skips unknown filenames intentionally.
            continue
        if recorded_sha != actual_sha:
            issues.append(
                SQLiteIntegrityIssue(
                    path=path,
                    expected_sha256=recorded_sha,
                    actual_sha256=actual_sha,
                    reason="raw JSON changed since last rebuild-cache run",
                )
            )
    return tuple(issues)
