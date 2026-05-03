from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.cli import migrate_cache_command
from baibai_loop.screening.migrate import migrate_cache, plan_migration


class PlanMigrationTests(unittest.TestCase):
    def test_plan_lists_every_file_with_relative_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "legacy"
            destination = root / "raw"
            (source / "jquants").mkdir(parents=True)
            (source / "jquants" / "a.json").write_text("{}", encoding="utf-8")
            (source / "edinet" / "documents").mkdir(parents=True)
            (source / "edinet" / "documents" / "2026-04-24.json").write_text(
                "[]", encoding="utf-8"
            )

            plan = plan_migration(source, destination)

            paths = sorted((entry.source, entry.destination) for entry in plan)
            self.assertEqual(
                paths,
                sorted(
                    [
                        (
                            source / "edinet" / "documents" / "2026-04-24.json",
                            destination / "edinet" / "documents" / "2026-04-24.json",
                        ),
                        (
                            source / "jquants" / "a.json",
                            destination / "jquants" / "a.json",
                        ),
                    ]
                ),
            )

    def test_plan_returns_empty_when_source_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(plan_migration(root / "missing", root / "raw"), ())


class MigrateCacheTests(unittest.TestCase):
    def test_moves_files_preserving_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "legacy"
            destination = root / "raw"
            (source / "jquants").mkdir(parents=True)
            payload = json.dumps({"hello": "world"})
            (source / "jquants" / "get_eq_master.json").write_text(payload, encoding="utf-8")

            result = migrate_cache(source, destination)

            self.assertEqual(len(result.moved), 1)
            self.assertEqual(len(result.skipped), 0)
            self.assertEqual(
                (destination / "jquants" / "get_eq_master.json").read_text(encoding="utf-8"),
                payload,
            )
            self.assertFalse(source.exists())

    def test_skips_existing_destination_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "legacy"
            destination = root / "raw"
            (source / "jquants").mkdir(parents=True)
            (source / "jquants" / "a.json").write_text("source", encoding="utf-8")
            (destination / "jquants").mkdir(parents=True)
            (destination / "jquants" / "a.json").write_text("preexisting", encoding="utf-8")

            result = migrate_cache(source, destination)

            self.assertEqual(len(result.moved), 0)
            self.assertEqual(len(result.skipped), 1)
            self.assertEqual(
                (destination / "jquants" / "a.json").read_text(encoding="utf-8"),
                "preexisting",
            )
            self.assertTrue((source / "jquants" / "a.json").exists())

    def test_dry_run_reports_plan_without_touching_filesystem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "legacy"
            destination = root / "raw"
            (source / "jquants").mkdir(parents=True)
            (source / "jquants" / "a.json").write_text("payload", encoding="utf-8")

            result = migrate_cache(source, destination, dry_run=True)

            self.assertEqual(len(result.moved), 1)
            self.assertEqual(result.moved_bytes, len(b"payload"))
            self.assertTrue((source / "jquants" / "a.json").exists())
            self.assertFalse((destination / "jquants" / "a.json").exists())

    def test_partial_rerun_resumes_remaining_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "legacy"
            destination = root / "raw"
            (source / "jquants").mkdir(parents=True)
            (source / "jquants" / "a.json").write_text("a", encoding="utf-8")
            (source / "jquants" / "b.json").write_text("b", encoding="utf-8")
            (destination / "jquants").mkdir(parents=True)
            (destination / "jquants" / "a.json").write_text("a", encoding="utf-8")

            result = migrate_cache(source, destination)

            self.assertEqual({entry.source.name for entry in result.moved}, {"b.json"})
            self.assertEqual({entry.source.name for entry in result.skipped}, {"a.json"})
            self.assertTrue((destination / "jquants" / "b.json").exists())


class MigrateCacheCommandTests(unittest.TestCase):
    def test_returns_zero_when_source_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout = io.StringIO()
            exit_code = migrate_cache_command(
                source=root / "missing",
                destination=root / "raw",
                stdout=stdout,
            )
            self.assertEqual(exit_code, 0)
            self.assertIn("nothing to migrate", stdout.getvalue())

    def test_reports_moved_and_skipped_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "legacy"
            destination = root / "raw"
            (source / "jquants").mkdir(parents=True)
            (source / "jquants" / "a.json").write_text("aa", encoding="utf-8")

            stdout = io.StringIO()
            exit_code = migrate_cache_command(
                source=source, destination=destination, stdout=stdout
            )
            self.assertEqual(exit_code, 0)
            output = stdout.getvalue()
            self.assertIn("moved 1 files", output)
            self.assertIn("2 bytes", output)
            self.assertIn("skipped 0 pre-existing", output)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
