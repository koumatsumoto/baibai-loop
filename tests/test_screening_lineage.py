from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.config import ScreeningConfig
from baibai_loop.screening.lineage import (
    build_provider_settings,
    build_run_id,
    compute_cache_manifest,
    compute_cache_manifest_hash,
    compute_config_hash,
    compute_sqlite_summary,
    write_manifest,
)
from baibai_loop.screening.render import JST
from baibai_loop.screening.sqlite_cache import open_connection


class ScreeningLineageTests(unittest.TestCase):
    def test_config_hash_is_stable_for_same_traceable_inputs(self) -> None:
        config = ScreeningConfig(
            "token-a",
            "key-a",
            cache_dir=Path(".cache/screening"),
            jpx_regulation_urls={"取引停止": "https://www.jpx.co.jp/example.csv"},
        )
        provider_settings = build_provider_settings(config)

        self.assertEqual(
            compute_config_hash(config, provider_settings),
            compute_config_hash(config, provider_settings),
        )

    def test_config_hash_ignores_secrets(self) -> None:
        config_a = ScreeningConfig(
            "token-a",
            "key-a",
            cache_dir=Path(".cache/screening"),
            jpx_regulation_urls={"取引停止": "https://www.jpx.co.jp/example.csv"},
        )
        config_b = ScreeningConfig(
            "token-b",
            "key-b",
            cache_dir=Path(".cache/screening"),
            jpx_regulation_urls={"取引停止": "https://www.jpx.co.jp/example.csv"},
        )

        self.assertEqual(
            compute_config_hash(config_a, build_provider_settings(config_a)),
            compute_config_hash(config_b, build_provider_settings(config_b)),
        )

    def test_config_hash_changes_for_provider_url_changes(self) -> None:
        config_a = ScreeningConfig(
            "token",
            "key",
            cache_dir=Path(".cache/screening"),
            jpx_regulation_urls={"取引停止": "https://www.jpx.co.jp/a.csv"},
        )
        config_b = ScreeningConfig(
            "token",
            "key",
            cache_dir=Path(".cache/screening"),
            jpx_regulation_urls={"取引停止": "https://www.jpx.co.jp/b.csv"},
        )

        self.assertNotEqual(
            compute_config_hash(config_a, build_provider_settings(config_a)),
            compute_config_hash(config_b, build_provider_settings(config_b)),
        )

    def test_config_hash_changes_for_edinet_api_base(self) -> None:
        config = ScreeningConfig("token", "key", cache_dir=Path(".cache/screening"))
        base_settings = build_provider_settings(config)
        base_hash = compute_config_hash(config, base_settings)

        edinet = base_settings["edinet"]
        assert isinstance(edinet, dict)
        altered_edinet: dict[str, object] = {
            **edinet,
            "api_base": "https://api-other.example.com/v2",
        }
        altered_settings: dict[str, object] = {**base_settings, "edinet": altered_edinet}

        self.assertNotEqual(base_hash, compute_config_hash(config, altered_settings))

    def test_config_hash_changes_for_jquants_methods(self) -> None:
        config = ScreeningConfig("token", "key", cache_dir=Path(".cache/screening"))
        base_settings = build_provider_settings(config)
        base_hash = compute_config_hash(config, base_settings)

        jquants = base_settings["jquants"]
        assert isinstance(jquants, dict)
        methods = jquants["methods"]
        assert isinstance(methods, list)
        altered_jquants: dict[str, object] = {
            **jquants,
            "methods": [*methods, "new_method"],
        }
        altered_settings: dict[str, object] = {**base_settings, "jquants": altered_jquants}

        self.assertNotEqual(base_hash, compute_config_hash(config, altered_settings))

    def test_asof_date_changes_run_id_but_not_config_hash(self) -> None:
        config = ScreeningConfig("token", "key", cache_dir=Path(".cache/screening"))
        config_hash = compute_config_hash(config, build_provider_settings(config))

        self.assertEqual(config_hash, compute_config_hash(config, build_provider_settings(config)))
        self.assertNotEqual(
            build_run_id(date(2026, 4, 24), config_hash),
            build_run_id(date(2026, 4, 25), config_hash),
        )

    def test_cache_manifest_hash_is_stable_and_excludes_manifest_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "jquants").mkdir()
            (root / "jquants" / "get_eq_master.json").write_text("{}", encoding="utf-8")
            (root / "manifests").mkdir()
            (root / "manifests" / "old.json").write_text("changes", encoding="utf-8")

            manifest = compute_cache_manifest(root)
            manifest_hash = compute_cache_manifest_hash(manifest)
            (root / "manifests" / "old.json").write_text("changed again", encoding="utf-8")

            self.assertEqual(
                manifest_hash, compute_cache_manifest_hash(compute_cache_manifest(root))
            )
            self.assertEqual(
                [record.path for record in manifest.files], ["jquants/get_eq_master.json"]
            )

    def test_write_manifest_records_hashes_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache_file = root / "edinet" / "metrics.json"
            cache_file.parent.mkdir()
            cache_file.write_text("[]", encoding="utf-8")
            manifest = compute_cache_manifest(root)
            manifest_hash = compute_cache_manifest_hash(manifest)
            path = root / "manifests" / "screening-20260424-a1b2c3d4.json"

            write_manifest(
                path,
                manifest,
                run_id="screening-20260424-a1b2c3d4",
                asof_date=date(2026, 4, 24),
                config_hash="a1b2c3d4e5f6a7b8",
                manifest_hash=manifest_hash,
                generated_at=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
            )

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["run_id"], "screening-20260424-a1b2c3d4")
            self.assertEqual(payload["asof_date"], "2026-04-24")
            self.assertEqual(payload["cache_root"], root.as_posix())
            self.assertEqual(payload["config_hash"], "a1b2c3d4e5f6a7b8")
            self.assertEqual(payload["cache_manifest_hash"], manifest_hash)
            self.assertEqual(payload["files"][0]["path"], "edinet/metrics.json")
            self.assertNotIn(
                "sqlite_cache",
                payload,
                "missing SQLite path should leave the manifest free of stale lineage",
            )

    def test_compute_sqlite_summary_returns_none_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(compute_sqlite_summary(Path(tmpdir) / "missing.sqlite"))
            self.assertIsNone(compute_sqlite_summary(None))

    def test_compute_sqlite_summary_records_schema_version_and_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.execute(
                "INSERT INTO source_coverage("
                "source, operation, coverage_key, coverage_start, coverage_end, "
                "requested_start, requested_end, params_json, fetched_at_utc, record_count"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "jquants_master_snapshots",
                    "get_eq_master",
                    "latest",
                    "2026-04-24",
                    "2026-04-24",
                    "2026-04-24",
                    "2026-04-24",
                    "{}",
                    "2026-04-24T00:00:00+00:00",
                    4445,
                ),
            )
            conn.commit()
            conn.close()

            summary = compute_sqlite_summary(sqlite_path)

            self.assertIsNotNone(summary)
            assert summary is not None  # narrow for type checker
            self.assertEqual(summary["path"], sqlite_path.as_posix())
            self.assertIn(summary["schema_version"], {"v8"})
            self.assertEqual(
                summary["coverage"],
                [{"source": "jquants_master_snapshots", "windows": 1, "records": 4445}],
            )

    def test_write_manifest_includes_sqlite_summary_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sqlite_path = root / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.commit()
            conn.close()

            manifest = compute_cache_manifest(root)
            manifest_hash = compute_cache_manifest_hash(manifest)
            path = root / "manifests" / "screening-20260424-a1b2c3d4.json"

            write_manifest(
                path,
                manifest,
                run_id="screening-20260424-a1b2c3d4",
                asof_date=date(2026, 4, 24),
                config_hash="a1b2c3d4e5f6a7b8",
                manifest_hash=manifest_hash,
                generated_at=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
                sqlite_path=sqlite_path,
            )

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("sqlite_cache", payload)
            self.assertEqual(payload["sqlite_cache"]["path"], sqlite_path.as_posix())
