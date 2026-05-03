from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.cli import verify_raw_cache_command
from baibai_loop.screening.sqlite_cache import open_connection
from baibai_loop.screening.verify import (
    DEFAULT_MAX_FILE_SIZE_MB,
    verify_raw_cache,
)


def _write_json_file(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _record_in_raw_imports(sqlite_path: Path, file_path: Path, sha256: str) -> None:
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_connection(sqlite_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO raw_imports("
            "source, path, sha256, imported_at_utc, record_count, min_date, max_date"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("test", file_path.as_posix(), sha256, "2026-05-03T00:00:00+00:00", 1, None, None),
        )
        conn.commit()
    finally:
        conn.close()


class VerifyRawCacheTests(unittest.TestCase):
    def test_returns_clean_result_for_missing_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = verify_raw_cache(Path(tmp) / "missing")
            self.assertEqual(result.file_count, 0)
            self.assertFalse(result.has_failures)

    def test_passes_when_all_files_below_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            _write_json_file(raw / "jquants" / "small.json", [{"x": 1}])

            result = verify_raw_cache(raw, max_size_mb=DEFAULT_MAX_FILE_SIZE_MB)

            self.assertEqual(result.file_count, 1)
            self.assertEqual(result.size_violations, ())
            self.assertFalse(result.has_failures)

    def test_flags_files_at_or_above_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            big_path = raw / "jquants" / "big.json"
            big_path.parent.mkdir(parents=True)
            big_path.write_bytes(b"x" * (1024 * 1024 + 1))  # >1MB

            result = verify_raw_cache(raw, max_size_mb=1)

            self.assertEqual(len(result.size_violations), 1)
            self.assertEqual(result.size_violations[0].path, big_path)
            self.assertTrue(result.has_failures)

    def test_excludes_manifests_subdir_from_walk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            manifest_path = raw / "manifests" / "run-1.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_bytes(b"x" * (1024 * 1024 + 1))

            result = verify_raw_cache(raw, max_size_mb=1)

            self.assertEqual(result.file_count, 0)
            self.assertEqual(result.size_violations, ())

    def test_skips_sqlite_check_when_db_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            _write_json_file(raw / "jquants" / "a.json", [])

            result = verify_raw_cache(raw, sqlite_path=Path(tmp) / "missing.sqlite")

            self.assertEqual(result.sqlite_issues, ())

    def test_flags_sha256_mismatch_against_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            file_path = raw / "jquants" / "a.json"
            _write_json_file(file_path, [{"x": 1}])
            stale_sha = "0" * 64
            _record_in_raw_imports(sqlite_path, file_path, stale_sha)

            result = verify_raw_cache(raw, sqlite_path=sqlite_path)

            self.assertEqual(len(result.sqlite_issues), 1)
            issue = result.sqlite_issues[0]
            self.assertEqual(issue.expected_sha256, stale_sha)
            self.assertNotEqual(issue.actual_sha256, stale_sha)
            self.assertTrue(result.has_failures)

    def test_passes_when_sha256_matches_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            file_path = raw / "jquants" / "a.json"
            _write_json_file(file_path, [{"x": 1}])
            actual_sha = hashlib.sha256(file_path.read_bytes()).hexdigest()
            _record_in_raw_imports(sqlite_path, file_path, actual_sha)

            result = verify_raw_cache(raw, sqlite_path=sqlite_path)

            self.assertEqual(result.sqlite_issues, ())
            self.assertFalse(result.has_failures)


class VerifyRawCacheCommandTests(unittest.TestCase):
    def test_returns_zero_for_clean_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            _write_json_file(raw / "jquants" / "a.json", [])

            stdout = io.StringIO()
            exit_code = verify_raw_cache_command(
                raw_dir=raw,
                max_size_mb=DEFAULT_MAX_FILE_SIZE_MB,
                sqlite_path=Path(tmp) / "missing.sqlite",
                stdout=stdout,
            )

            self.assertEqual(exit_code, 0)
            self.assertIn("verified 1 files", stdout.getvalue())

    def test_returns_one_when_size_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            big = raw / "jquants" / "big.json"
            big.parent.mkdir(parents=True)
            big.write_bytes(b"x" * (1024 * 1024 + 1))

            stdout = io.StringIO()
            exit_code = verify_raw_cache_command(
                raw_dir=raw,
                max_size_mb=1,
                sqlite_path=Path(tmp) / "missing.sqlite",
                stdout=stdout,
            )

            self.assertEqual(exit_code, 1)
            self.assertIn("size violations", stdout.getvalue())

    def test_returns_one_when_raw_dir_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            exit_code = verify_raw_cache_command(
                raw_dir=Path(tmp) / "missing",
                max_size_mb=DEFAULT_MAX_FILE_SIZE_MB,
                sqlite_path=Path(tmp) / "missing.sqlite",
                stdout=stdout,
            )
            self.assertEqual(exit_code, 1)


if __name__ == "__main__":  # pragma: no cover
    # sqlite3 import is required by the SHA-256 fixture; reference it so the
    # unused-import lint stays quiet without dropping the smoke check.
    assert sqlite3.sqlite_version
    unittest.main()
