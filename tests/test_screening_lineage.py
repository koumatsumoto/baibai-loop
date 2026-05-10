from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from baibai_loop.screening.lineage import build_run_id, compute_sqlite_summary
from baibai_loop.screening.sqlite_cache import SQLITE_SCHEMA_VERSION, open_connection


class ScreeningLineageTests(unittest.TestCase):
    def test_run_id_is_asof_based(self) -> None:
        self.assertEqual(build_run_id(date(2026, 4, 24)), "screening-20260424")
        self.assertNotEqual(build_run_id(date(2026, 4, 24)), build_run_id(date(2026, 4, 25)))

    def test_compute_sqlite_summary_returns_none_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(compute_sqlite_summary(Path(tmpdir) / "missing.sqlite"))
            self.assertIsNone(compute_sqlite_summary(None))

    def test_compute_sqlite_summary_records_current_user_version_and_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.execute(
                "INSERT INTO source_coverage("
                "source, coverage_key, coverage_start, coverage_end, fetched_at_utc, "
                "record_count, status, error"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "jquants_master_snapshots",
                    "latest",
                    "2026-04-24",
                    "2026-04-24",
                    "2026-04-24T00:00:00+00:00",
                    4445,
                    "ok",
                    None,
                ),
            )
            conn.execute(
                "INSERT INTO source_coverage("
                "source, coverage_key, coverage_start, coverage_end, fetched_at_utc, "
                "record_count, status, error"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "edinet_metrics",
                    "2026-04-24",
                    "2026-04-24",
                    "2026-04-24",
                    "2026-04-24T00:00:00+00:00",
                    0,
                    "failed",
                    "no filings selected: a, b, c",
                ),
            )
            conn.commit()
            conn.close()

            summary = compute_sqlite_summary(sqlite_path)

            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertEqual(summary["path"], sqlite_path.as_posix())
            self.assertEqual(summary["user_version"], SQLITE_SCHEMA_VERSION)
            self.assertEqual(
                summary["coverage"],
                [
                    {
                        "source": "edinet_metrics",
                        "windows": 1,
                        "records": 0,
                        "statuses": ["failed"],
                        "non_ok_windows": 1,
                        "errors": ["no filings selected: a, b, c"],
                    },
                    {
                        "source": "jquants_master_snapshots",
                        "windows": 1,
                        "records": 4445,
                        "statuses": ["ok"],
                        "non_ok_windows": 0,
                        "errors": [],
                    },
                ],
            )


if __name__ == "__main__":
    unittest.main()
