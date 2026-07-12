from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import cast
from unittest.mock import patch

import yaml

from baibai_loop.foundation.yaml_io import safe_load

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.foundation.time import JST
from baibai_loop.screening import cli as screening_cli
from baibai_loop.screening.cli import (
    ProviderBundle,
    bootstrap_cache_command,
    extract_edinet_metrics_command,
    run_command,
    select_command,
)
from baibai_loop.screening.cli.run import _index_next_earnings
from baibai_loop.screening.config import ScreeningConfig
from baibai_loop.screening.providers import JQuantsProvider
from baibai_loop.screening.providers.edinet import (
    EdinetMetricRecord,
    EDINETProviderError,
    EDINETRateLimitError,
)
from baibai_loop.screening.providers.jpx import JPXProviderError, JPXRegulationSnapshot
from baibai_loop.screening.providers.jquants import (
    JQuantsDailyBar,
    JQuantsFinancialSummary,
    JQuantsMarketCalendarDay,
)
from baibai_loop.screening.render import build_output_path
from baibai_loop.screening.rule_config import load_screening_rules
from baibai_loop.screening.schema import SecurityMaster, TTMQuality
from baibai_loop.screening.sqlite_cache import store_edinet_metrics
from baibai_loop.screening.sqlite_reader import read_edinet_metrics


@dataclass
class FakeJQuantsProvider:
    business_day: bool = True
    calls: list[tuple[str, date | None, date | None]] = field(default_factory=list)

    def get_mkt_calendar(self, start: date, end: date) -> list[JQuantsMarketCalendarDay]:
        self.calls.append(("get_mkt_calendar", start, end))
        return [JQuantsMarketCalendarDay(day=start, is_business_day=self.business_day)]

    def get_eq_master(self) -> list[SecurityMaster]:
        self.calls.append(("get_eq_master", None, None))
        return [
            SecurityMaster(
                code="130A",
                name="Alpha",
                market_segment="Prime",
                sector_33="情報・通信業",
                is_common_stock=True,
            )
        ]

    def get_eq_bars_daily_range(self, start: date, end: date) -> list[JQuantsDailyBar]:
        self.calls.append(("get_eq_bars_daily_range", start, end))
        # Span >=800 days so listed_under_6_months (182) and short_history_flag (750)
        # checks both treat the fixture as an established listing.
        total = 800
        base = end - timedelta(days=total - 1)
        return [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=base + timedelta(days=index),
                close=100 + index,
                turnover_value=300_000_000.0,
            )
            for index in range(total)
        ]

    def get_fin_summary_range(self, start: date, end: date) -> list[JQuantsFinancialSummary]:
        self.calls.append(("get_fin_summary_range", start, end))
        return [
            JQuantsFinancialSummary(
                ticker="130A",
                disclosed_at=date(2025, 12, 31),
                forecast_eps=20.0,
                eps_ttm=15.0,
                bps=120.0,
                shares_outstanding=400_000_000.0,
                sales=1000.0,
                operating_profit=100.0,
                cash_eq=200_000_000_000.0,
                total_assets=1_000_000_000_000.0,
                equity=600_000_000_000.0,
                ordinary_profit=None,
                profit=None,
            ),
            JQuantsFinancialSummary(
                ticker="130A",
                disclosed_at=date(2026, 3, 31),
                forecast_eps=22.0,
                eps_ttm=18.0,
                bps=130.0,
                shares_outstanding=400_000_000.0,
                sales=1100.0,
                operating_profit=110.0,
                cash_eq=210_000_000_000.0,
                total_assets=1_000_000_000_000.0,
                equity=600_000_000_000.0,
                ordinary_profit=None,
                profit=None,
            ),
        ]

    def get_eq_earnings_cal(self, start: date, end: date) -> list[dict[str, str]]:
        self.calls.append(("get_eq_earnings_cal", start, end))
        return []


@dataclass
class FakeEDINETProvider:
    documents: list[dict[str, object]] | None = None
    zip_by_doc_id: dict[str, bytes] | None = None
    bootstrap_calls: list[tuple[date, date]] = field(default_factory=list)

    def load_metric_records(self, asof_date: date) -> dict[str, EdinetMetricRecord]:
        del asof_date
        return {
            "130A": EdinetMetricRecord(
                ticker="130A",
                sales_ttm=1000.0,
                ocf_ttm=100.0,
                debt=50.0,
                cash=20.0,
                ebitda_ttm=120.0,
                consolidation_basis="consolidated",
                ttm_quality_ev_ebitda=TTMQuality.EXACT,
                ttm_quality_p_s=TTMQuality.APPROXIMATED,
                ttm_quality_pcfr=TTMQuality.UNAVAILABLE,
                source_doc_id="S100TEST",
                source_submit_datetime="2025-10-15 12:00",
            )
        }

    def list_documents(self, on_date: date) -> list[dict[str, object]]:
        del on_date
        return list(self.documents or [])

    def download_csv_zip(self, doc_id: str) -> bytes:
        return (self.zip_by_doc_id or {})[doc_id]

    def bootstrap_cache(self, start: date, end: date) -> dict[str, int]:
        self.bootstrap_calls.append((start, end))
        return {"ok": 1}


class _FreshnessWarningEDINETProvider(FakeEDINETProvider):
    def load_metric_records(self, asof_date: date) -> dict[str, EdinetMetricRecord]:
        del asof_date
        return {
            "130A": EdinetMetricRecord(
                ticker="130A",
                debt=20_000_000_000.0,
                cash=150_000_000_000.0,
                net_cash=130_000_000_000.0,
                ebitda_ttm=30_000_000_000.0,
                ttm_quality_net_cash=TTMQuality.EXACT,
                ttm_quality_ev_ebitda=TTMQuality.EXACT,
                source_doc_id="S100TEST",
                source_submit_datetime="2025-10-15 12:00",
            )
        }


class _FailingEDINETProvider(FakeEDINETProvider):
    def load_metric_records(self, asof_date: date) -> dict[str, EdinetMetricRecord]:
        del asof_date
        raise EDINETProviderError("broken metrics cache")


class _CorruptSQLiteJQuantsProvider(FakeJQuantsProvider):
    def get_eq_master(self) -> list[SecurityMaster]:
        raise sqlite3.DatabaseError("file is not a database")


@dataclass
class FakeJPXProvider:
    fail_bootstrap: bool = False
    cache_exists: bool = True
    snapshots_requested: int = 0
    source_names: tuple[str, ...] = (
        "上場廃止警告",
        "取引停止",
        "整理銘柄",
        "特別注意銘柄",
    )
    bootstrap_calls: list[date] = field(default_factory=list)

    def get_regulation_snapshot(self, asof_date: date) -> JPXRegulationSnapshot:
        del asof_date
        self.snapshots_requested += 1
        return JPXRegulationSnapshot(flags_by_ticker={}, source_names=self.source_names)

    def has_regulation_cache(self, asof_date: date) -> bool:
        del asof_date
        return self.cache_exists

    def bootstrap_cache(self, asof_date: date) -> dict[str, int]:
        self.bootstrap_calls.append(asof_date)
        if self.fail_bootstrap:
            raise JPXProviderError("missing jpx source")
        return {"ok": 1}


class ScreeningCliTests(unittest.TestCase):
    def test_main_run_fails_on_incomplete_sqlite_before_provider_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path.cwd()
            stderr = io.StringIO()
            try:
                os.chdir(Path(tmpdir))
                with (
                    patch.dict(os.environ, {"JQUANTS_API_KEY": "token"}),
                    patch.object(
                        JQuantsProvider,
                        "_get_client",
                        side_effect=AssertionError("provider fetch must not be used"),
                    ),
                    contextlib.redirect_stderr(stderr),
                ):
                    exit_code = screening_cli.main(["run", "--asof", "2026-05-08"])
            finally:
                os.chdir(cwd)

            self.assertEqual(exit_code, 1)
            self.assertIn("SQLite cache coverage incomplete", stderr.getvalue())
            self.assertIn("will not fall back to raw JSON or provider APIs", stderr.getvalue())

    def test_run_command_writes_candidates_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path.cwd()
            try:
                os_path = Path(tmpdir)
                os.chdir(os_path)
                config = ScreeningConfig("token", "key", cache_dir=Path(".cache/screening"))
                providers = ProviderBundle(
                    jquants=FakeJQuantsProvider(),
                    edinet=FakeEDINETProvider(),
                    jpx=FakeJPXProvider(),
                )
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = run_command(
                        date(2026, 4, 24),
                        config,
                        providers,
                        now=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
                    )
                self.assertEqual(exit_code, 2)
                self.assertIn("screening run done: status=partial warning", stdout.getvalue())
                self.assertIn("screening run partial warning reasons:", stdout.getvalue())
                output_path = build_output_path(date(2026, 4, 24))
                self.assertTrue(output_path.exists())
                rendered = output_path.read_text(encoding="utf-8")
                self.assertIn('run_date: "2026-04-24"', rendered)
                self.assertIn("ttm_quality_counts:", rendered)
                payload = safe_load(rendered)
                self.assertEqual(
                    payload["data_sources"],
                    [
                        "j-quants-light",
                        "jpx-public-regulation",
                        "edinet-preprocessed-metrics",
                    ],
                )
                self.assertIn(
                    "EDINET preprocessed metrics: loaded", payload["provider_status_lines"]
                )
                self.assertEqual(payload["run_id"], "screening-20260424")
                self.assertEqual(payload["filters"]["scope"], "all-common-stocks")
                self.assertEqual(len(payload["candidates"]), 1)
                self.assertEqual(payload["candidates"][0]["ticker"], "130A")
                self.assertEqual(payload["candidates"][0]["evidence_hits"], [])
                manifest_path = Path(".cache/screening/manifests") / f"{payload['run_id']}.json"
                self.assertFalse(manifest_path.exists())
            finally:
                os.chdir(cwd)

    def test_run_command_can_write_custom_output_path_with_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path.cwd()
            try:
                os_path = Path(tmpdir)
                import os

                os.chdir(os_path)
                config = ScreeningConfig("token", "key", cache_dir=Path(".cache/screening"))
                providers = ProviderBundle(
                    jquants=FakeJQuantsProvider(),
                    edinet=FakeEDINETProvider(),
                    jpx=FakeJPXProvider(),
                )
                output_path = Path("records/02-candidates/e2e/candidates.yaml")
                exit_code = run_command(
                    date(2026, 4, 24),
                    config,
                    providers,
                    now=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
                    output_path=output_path,
                )
                self.assertEqual(exit_code, 2)
                self.assertTrue(output_path.exists())
                rejected = run_command(
                    date(2026, 4, 24),
                    config,
                    providers,
                    now=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
                    output_path=output_path,
                )
                self.assertEqual(rejected, 1)
                overwritten = run_command(
                    date(2026, 4, 24),
                    config,
                    providers,
                    now=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
                    output_path=output_path,
                    force=True,
                )
                self.assertEqual(overwritten, 2)
            finally:
                os.chdir(cwd)

    def test_run_command_does_not_write_universe_snapshot_for_scratch_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path.cwd()
            try:
                os_path = Path(tmpdir)
                import os

                os.chdir(os_path)
                config = ScreeningConfig("token", "key", cache_dir=Path(".cache/screening"))
                providers = ProviderBundle(
                    jquants=FakeJQuantsProvider(),
                    edinet=FakeEDINETProvider(),
                    jpx=FakeJPXProvider(),
                )
                output_path = (Path.cwd() / ".cache/simplify/candidates.yaml").resolve()

                exit_code = run_command(
                    date(2026, 4, 24),
                    config,
                    providers,
                    now=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
                    output_path=output_path,
                )

                self.assertEqual(exit_code, 2)
                payload = safe_load(output_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["run_id"], "screening-20260424")
                self.assertEqual(output_path.parent, Path(".cache/simplify").resolve())
            finally:
                os.chdir(cwd)

    def test_run_command_fails_when_required_edinet_provider_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path.cwd()
            try:
                os_path = Path(tmpdir)
                import os

                os.chdir(os_path)
                config = ScreeningConfig("token", None, cache_dir=Path(".cache/screening"))
                providers = ProviderBundle(
                    jquants=FakeJQuantsProvider(),
                    edinet=None,
                    jpx=FakeJPXProvider(),
                )
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    exit_code = run_command(
                        date(2026, 4, 24),
                        config,
                        providers,
                        now=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
                    )
                self.assertEqual(exit_code, 1)
                self.assertIn("EDINET preprocessed metrics provider is required", stderr.getvalue())
                self.assertFalse(build_output_path(date(2026, 4, 24)).exists())
            finally:
                os.chdir(cwd)

    def test_run_command_fails_when_required_edinet_load_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path.cwd()
            try:
                os_path = Path(tmpdir)
                import os

                os.chdir(os_path)
                config = ScreeningConfig("token", "key", cache_dir=Path(".cache/screening"))
                providers = ProviderBundle(
                    jquants=FakeJQuantsProvider(),
                    edinet=_FailingEDINETProvider(),
                    jpx=FakeJPXProvider(),
                )
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    exit_code = run_command(
                        date(2026, 4, 24),
                        config,
                        providers,
                        now=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
                    )
                self.assertEqual(exit_code, 1)
                self.assertIn("EDINET load_metric_records failed", stderr.getvalue())
                self.assertFalse(build_output_path(date(2026, 4, 24)).exists())
            finally:
                os.chdir(cwd)

    def test_run_command_emits_edinet_freshness_warnings_from_disclosure_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path.cwd()
            try:
                os_path = Path(tmpdir)
                import os

                os.chdir(os_path)
                cache_dir = Path(".cache/screening")
                disclosure_path = cache_dir / "disclosures" / "tdnet.json"
                disclosure_path.parent.mkdir(parents=True)
                disclosure_path.write_text(
                    json.dumps(
                        [
                            {
                                "Code": "130A0",
                                "Date": "2026-03-03",
                                "Title": "資金の借入に関するお知らせ",
                                "Source": "tdnet",
                            }
                        ]
                    ),
                    encoding="utf-8",
                )
                config = ScreeningConfig("token", "key", cache_dir=cache_dir)
                providers = ProviderBundle(
                    jquants=FakeJQuantsProvider(),
                    edinet=_FreshnessWarningEDINETProvider(),
                    jpx=FakeJPXProvider(),
                )

                exit_code = run_command(
                    date(2026, 4, 24),
                    config,
                    providers,
                    now=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
                )

                self.assertEqual(exit_code, 2)
                payload = safe_load(build_output_path(date(2026, 4, 24)).read_text())
                self.assertIn("disclosure-title-events", payload["data_sources"])
                self.assertIn(
                    "Disclosure title material-event scan: 1 events from 1 files "
                    "(skipped=0, unsupported=0, errors=0)",
                    payload["provider_status_lines"],
                )
                warnings = payload["candidates"][0]["freshness_warnings"]
                self.assertEqual(warnings[0]["event_kind"], "borrowing")
                self.assertEqual(warnings[0]["stale_metric"], "edinet_metrics")
                self.assertEqual(
                    payload["candidates"][0]["metrics"]["edinet_freshness_warning_count"], 1
                )
            finally:
                os.chdir(cwd)

    def test_run_command_reports_disclosure_scan_coverage_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path.cwd()
            try:
                os_path = Path(tmpdir)
                import os

                os.chdir(os_path)
                cache_dir = Path(".cache/screening")
                disclosure_dir = cache_dir / "disclosures"
                disclosure_dir.mkdir(parents=True)
                (disclosure_dir / "broken.json").write_text("{", encoding="utf-8")
                (disclosure_dir / "mixed.json").write_text(
                    json.dumps(
                        {
                            "events": [
                                {"Code": "130A0", "Date": "2026-03-03"},
                                "not-a-record",
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                config = ScreeningConfig("token", "key", cache_dir=cache_dir)
                providers = ProviderBundle(
                    jquants=FakeJQuantsProvider(),
                    edinet=_FreshnessWarningEDINETProvider(),
                    jpx=FakeJPXProvider(),
                )

                exit_code = run_command(
                    date(2026, 4, 24),
                    config,
                    providers,
                    now=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
                )

                self.assertEqual(exit_code, 2)
                payload = safe_load(build_output_path(date(2026, 4, 24)).read_text())
                self.assertIn(
                    "Disclosure title material-event scan: 0 events from 2 files "
                    "(skipped=1, unsupported=1, errors=1)",
                    payload["provider_status_lines"],
                )
                self.assertIn("Disclosure title scan 読み込み失敗: 1 件", payload["fallback_lines"])
                self.assertIn(
                    "Disclosure title scan 必須 key 欠損/不正 record: 1 件",
                    payload["fallback_lines"],
                )
                self.assertIn(
                    "Disclosure title scan 未対応 record/layout: 1 件",
                    payload["fallback_lines"],
                )
            finally:
                os.chdir(cwd)

    def test_run_command_prints_notices_for_non_partial_fallback_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path.cwd()
            try:
                os_path = Path(tmpdir)
                import os

                os.chdir(os_path)
                cache_dir = Path(".cache/screening")
                disclosure_dir = cache_dir / "disclosures"
                disclosure_dir.mkdir(parents=True)
                (disclosure_dir / "mixed.json").write_text(
                    json.dumps({"events": [{"Code": "130A0", "Date": "2026-03-03"}]}),
                    encoding="utf-8",
                )
                config = ScreeningConfig("token", "key", cache_dir=cache_dir)
                providers = ProviderBundle(
                    jquants=FakeJQuantsProvider(),
                    edinet=_FreshnessWarningEDINETProvider(),
                    jpx=FakeJPXProvider(),
                )
                rules = load_screening_rules(config.rules_path)
                rules = rules.model_copy(
                    update={
                        "quality": rules.quality.model_copy(
                            update={
                                "partial_warning_ttm_count": 999,
                                "partial_warning_ttm_ratio": 999.0,
                                "partial_warning_yoy_missing_ratio": 999.0,
                            }
                        )
                    }
                )
                stdout = io.StringIO()

                exit_code = run_command(
                    date(2026, 4, 24),
                    config,
                    providers,
                    now=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
                    stdout=stdout,
                    rules=rules,
                )

                self.assertEqual(exit_code, 0)
                output = stdout.getvalue()
                self.assertIn("screening run notices:", output)
                self.assertNotIn("screening run partial warning reasons:", output)
            finally:
                os.chdir(cwd)

    def test_run_command_rejects_non_business_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path.cwd()
            try:
                import os

                os.chdir(tmpdir)
                config = ScreeningConfig("token", "key", cache_dir=Path(".cache/screening"))
                providers = ProviderBundle(
                    jquants=FakeJQuantsProvider(business_day=False),
                    edinet=FakeEDINETProvider(),
                    jpx=FakeJPXProvider(),
                )
                exit_code = run_command(date(2026, 4, 24), config, providers)
                self.assertEqual(exit_code, 1)
            finally:
                os.chdir(cwd)

    def test_run_command_fails_stale_jpx_backfill_without_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path.cwd()
            try:
                import os

                os.chdir(tmpdir)
                config = ScreeningConfig("token", "key", cache_dir=Path(".cache/screening"))
                jpx = FakeJPXProvider(cache_exists=False)
                providers = ProviderBundle(
                    jquants=FakeJQuantsProvider(),
                    edinet=FakeEDINETProvider(),
                    jpx=jpx,
                )
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    exit_code = run_command(
                        date(2026, 1, 15),
                        config,
                        providers,
                        now=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
                    )
                self.assertEqual(exit_code, 1)
                self.assertEqual(jpx.snapshots_requested, 0)
                self.assertIn("--allow-stale-jpx", stderr.getvalue())
            finally:
                os.chdir(cwd)

    def test_run_command_fails_jpx_backfill_just_over_stale_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path.cwd()
            try:
                import os

                os.chdir(tmpdir)
                config = ScreeningConfig("token", "key", cache_dir=Path(".cache/screening"))
                jpx = FakeJPXProvider(cache_exists=False)
                providers = ProviderBundle(
                    jquants=FakeJQuantsProvider(),
                    edinet=FakeEDINETProvider(),
                    jpx=jpx,
                )
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    exit_code = run_command(
                        date(2026, 4, 14),
                        config,
                        providers,
                        now=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
                    )
                self.assertEqual(exit_code, 1)
                self.assertEqual(jpx.snapshots_requested, 0)
                self.assertIn("--allow-stale-jpx", stderr.getvalue())
            finally:
                os.chdir(cwd)

    def test_run_command_allows_stale_jpx_backfill_when_flag_is_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path.cwd()
            try:
                import os

                os.chdir(tmpdir)
                config = ScreeningConfig("token", "key", cache_dir=Path(".cache/screening"))
                jpx = FakeJPXProvider(cache_exists=False)
                providers = ProviderBundle(
                    jquants=FakeJQuantsProvider(),
                    edinet=FakeEDINETProvider(),
                    jpx=jpx,
                )
                exit_code = run_command(
                    date(2026, 1, 15),
                    config,
                    providers,
                    now=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
                    allow_stale_jpx=True,
                )
                # The fixture intentionally lacks enough YoY inputs, so the
                # command succeeds with the existing partial-warning exit code.
                self.assertEqual(exit_code, 2)
                self.assertEqual(jpx.snapshots_requested, 1)
            finally:
                os.chdir(cwd)

    def test_run_command_allows_jpx_backfill_at_stale_boundary_without_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path.cwd()
            try:
                import os

                os.chdir(tmpdir)
                config = ScreeningConfig("token", "key", cache_dir=Path(".cache/screening"))
                jpx = FakeJPXProvider(cache_exists=False)
                providers = ProviderBundle(
                    jquants=FakeJQuantsProvider(),
                    edinet=FakeEDINETProvider(),
                    jpx=jpx,
                )
                exit_code = run_command(
                    date(2026, 4, 15),
                    config,
                    providers,
                    now=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
                )
                self.assertEqual(exit_code, 2)
                self.assertEqual(jpx.snapshots_requested, 1)
            finally:
                os.chdir(cwd)

    def test_run_command_fails_when_required_jpx_source_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path.cwd()
            try:
                import os

                os.chdir(tmpdir)
                config = ScreeningConfig("token", "key", cache_dir=Path(".cache/screening"))
                providers = ProviderBundle(
                    jquants=FakeJQuantsProvider(),
                    edinet=FakeEDINETProvider(),
                    jpx=FakeJPXProvider(source_names=("特別注意銘柄", "整理銘柄")),
                )
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    exit_code = run_command(
                        date(2026, 4, 24),
                        config,
                        providers,
                        now=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
                    )
                self.assertEqual(exit_code, 1)
                self.assertIn("missing required JPX regulation sources", stderr.getvalue())
                self.assertFalse(build_output_path(date(2026, 4, 24)).exists())
            finally:
                os.chdir(cwd)

    def test_bootstrap_cache_command_fails_jpx_bootstrap_failure(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = bootstrap_cache_command(
                asof_date=date(2026, 5, 8),
                providers=ProviderBundle(
                    jquants=FakeJQuantsProvider(),
                    edinet=FakeEDINETProvider(),
                    jpx=FakeJPXProvider(fail_bootstrap=True),
                ),
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("JPXProviderError: missing jpx source", stderr.getvalue())

    def test_bootstrap_cache_command_fails_cleanly_on_sqlite_corruption(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = bootstrap_cache_command(
                asof_date=date(2026, 5, 8),
                providers=ProviderBundle(
                    jquants=_CorruptSQLiteJQuantsProvider(),
                    edinet=FakeEDINETProvider(),
                    jpx=FakeJPXProvider(),
                ),
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("DatabaseError: file is not a database", stderr.getvalue())

    def test_bootstrap_cache_command_asof_uses_source_specific_windows(self) -> None:
        asof = date(2026, 5, 8)
        jquants = FakeJQuantsProvider()
        edinet = FakeEDINETProvider()
        jpx = FakeJPXProvider()
        buffer = io.StringIO()

        exit_code = bootstrap_cache_command(
            asof_date=asof,
            providers=ProviderBundle(jquants=jquants, edinet=edinet, jpx=jpx),
            stdout=buffer,
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("bootstrap-cache jquants daily_bars", buffer.getvalue())
        self.assertIn("bootstrap-cache edinet documents", buffer.getvalue())
        self.assertIn("bootstrap-cache done", buffer.getvalue())
        self.assertIn(("get_eq_master", None, None), jquants.calls)
        self.assertIn(("get_eq_bars_daily_range", asof - timedelta(days=1200), asof), jquants.calls)
        self.assertIn(("get_fin_summary_range", asof - timedelta(days=730), asof), jquants.calls)
        self.assertIn(("get_eq_earnings_cal", asof, asof + timedelta(days=90)), jquants.calls)
        self.assertIn(("get_mkt_calendar", asof, asof), jquants.calls)
        self.assertEqual(edinet.bootstrap_calls, [(asof - timedelta(days=730), asof)])
        self.assertEqual(jpx.bootstrap_calls, [asof])

    def test_extract_edinet_metrics_command_writes_parsed_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            provider = FakeEDINETProvider(
                documents=[
                    {
                        "docID": "S100TEST",
                        "secCode": "96820",
                        "docTypeCode": "120",
                        "csvFlag": "1",
                        "xbrlFlag": "1",
                        "periodStart": "2025-04-01",
                        "periodEnd": "2026-03-31",
                        "submitDateTime": "2026-04-01 12:00",
                    }
                ],
                zip_by_doc_id={"S100TEST": _edinet_csv_zip()},
            )
            buffer = io.StringIO()
            exit_code = extract_edinet_metrics_command(
                asof_date=date(2026, 4, 24),
                lookback_days=0,
                provider=provider,
                sqlite_path=sqlite_path,
                stdout=buffer,
            )
            self.assertEqual(exit_code, 0)
            payload = read_edinet_metrics(sqlite_path, date(2026, 4, 24))
            assert payload is not None
            record = payload["9682"]
            self.assertEqual(record.ticker, "9682")
            self.assertEqual(record.net_cash, 600.0)
            self.assertEqual(record.fcf_ttm, 700.0)
            self.assertEqual(record.source_submit_datetime, "2026-04-01 12:00")
            self.assertEqual(record.source_period_start, date(2025, 4, 1))
            self.assertEqual(record.source_period_end, date(2026, 3, 31))
            self.assertIn("1 EDINET metric records", buffer.getvalue())

    def test_extract_edinet_metrics_command_returns_zero_for_quality_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            provider = FakeEDINETProvider(
                documents=[
                    {
                        "docID": "S100TEST",
                        "secCode": "96820",
                        "docTypeCode": "120",
                        "csvFlag": "1",
                        "xbrlFlag": "1",
                        "submitDateTime": "2026-04-01 12:00",
                    }
                ],
                zip_by_doc_id={"S100TEST": _edinet_csv_zip(include_debt=False)},
            )
            buffer = io.StringIO()
            exit_code = extract_edinet_metrics_command(
                asof_date=date(2026, 4, 24),
                lookback_days=0,
                provider=provider,
                sqlite_path=sqlite_path,
                stdout=buffer,
            )

            self.assertEqual(exit_code, 0)
            payload = read_edinet_metrics(sqlite_path, date(2026, 4, 24))
            assert payload is not None
            self.assertIn("debt_assumed_zero", payload["9682"].failure_reasons)
            self.assertIn("1 records with quality issues", buffer.getvalue())

    def test_extract_edinet_metrics_command_fails_when_no_filings_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            provider = FakeEDINETProvider(documents=[])
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 24),
                    lookback_days=0,
                    provider=provider,
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                )

            self.assertEqual(exit_code, 1)
            self.assertIn("no EDINET filings selected", stderr.getvalue())
            row = (
                sqlite3.connect(sqlite_path)
                .execute(
                    "SELECT status, error FROM source_coverage WHERE source = ?",
                    ("edinet_metrics",),
                )
                .fetchone()
            )
            self.assertEqual(row[0], "failed")
            self.assertIn("no EDINET filings selected", row[1])

    def test_extract_edinet_metrics_command_fails_closed_on_document_listing_error(self) -> None:
        class FailingListProvider(FakeEDINETProvider):
            def list_documents(self, on_date: date) -> list[dict[str, object]]:
                del on_date
                raise EDINETProviderError("temporary EDINET outage")

        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            asof = date(2026, 4, 24)
            store_edinet_metrics(
                sqlite_path,
                asof,
                [{"ticker": "9682", "sales_ttm": 1_000.0}],
                status="ok",
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = extract_edinet_metrics_command(
                    asof_date=asof,
                    lookback_days=0,
                    provider=FailingListProvider(),
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                )

            self.assertEqual(exit_code, 1)
            self.assertIn("EDINET document listing failed", stderr.getvalue())
            self.assertIsNone(read_edinet_metrics(sqlite_path, asof))
            row = (
                sqlite3.connect(sqlite_path)
                .execute(
                    "SELECT status, error FROM source_coverage WHERE source = ? "
                    "AND coverage_key = ?",
                    ("edinet_metrics", asof.isoformat()),
                )
                .fetchone()
            )
            self.assertEqual(row[0], "failed")
            self.assertIn("EDINET document listing failed", row[1])

    def test_extract_edinet_metrics_command_fails_on_invalid_document_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            asof = date(2026, 4, 24)
            store_edinet_metrics(
                sqlite_path,
                asof,
                [{"ticker": "9682", "sales_ttm": 1_000.0}],
                status="ok",
            )
            self.assertIsNotNone(read_edinet_metrics(sqlite_path, asof))
            provider = FakeEDINETProvider(
                documents=[
                    {
                        "docID": "S100TEST",
                        "secCode": "../../96820",
                        "docTypeCode": "120",
                        "csvFlag": "1",
                        "xbrlFlag": "1",
                    }
                ]
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = extract_edinet_metrics_command(
                    asof_date=asof,
                    lookback_days=0,
                    provider=provider,
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                )

            self.assertEqual(exit_code, 1)
            self.assertIn("invalid EDINET secCode", stderr.getvalue())
            self.assertIsNone(read_edinet_metrics(sqlite_path, asof))
            row = (
                sqlite3.connect(sqlite_path)
                .execute(
                    "SELECT status, error FROM source_coverage WHERE source = ? "
                    "AND coverage_key = ?",
                    ("edinet_metrics", asof.isoformat()),
                )
                .fetchone()
            )
            self.assertEqual(row[0], "failed")
            self.assertIn("EDINET document selection failed", row[1])

    def test_extract_edinet_metrics_command_fails_closed_on_csv_rate_limit(self) -> None:
        class RateLimitedZipProvider(FakeEDINETProvider):
            def download_csv_zip(self, doc_id: str) -> bytes:
                del doc_id
                raise EDINETRateLimitError("too many requests")

        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            asof = date(2026, 4, 24)
            store_edinet_metrics(
                sqlite_path,
                asof,
                [{"ticker": "9682", "sales_ttm": 1_000.0}],
                status="ok",
            )
            provider = RateLimitedZipProvider(
                documents=[
                    {
                        "docID": "S100TEST",
                        "secCode": "96820",
                        "docTypeCode": "120",
                        "csvFlag": "1",
                        "xbrlFlag": "1",
                    }
                ]
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = extract_edinet_metrics_command(
                    asof_date=asof,
                    lookback_days=0,
                    provider=provider,
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                )

            self.assertEqual(exit_code, 1)
            self.assertIn("EDINET CSV download rate limited", stderr.getvalue())
            self.assertIsNone(read_edinet_metrics(sqlite_path, asof))
            row = (
                sqlite3.connect(sqlite_path)
                .execute(
                    "SELECT status, error FROM source_coverage WHERE source = ? "
                    "AND coverage_key = ?",
                    ("edinet_metrics", asof.isoformat()),
                )
                .fetchone()
            )
            self.assertEqual(row[0], "failed")
            self.assertIn("EDINET CSV download rate limited", row[1])


class IndexNextEarningsTests(unittest.TestCase):
    def test_picks_earliest_future_announcement_per_ticker(self) -> None:
        records = [
            {"Code": "13010", "Date": "2026-05-13T00:00:00"},
            {"Code": "13010", "Date": "2026-08-13T00:00:00"},
            {"Code": "29140", "Date": "2026-05-08T00:00:00"},
        ]
        result = _index_next_earnings(records, date(2026, 4, 25))
        self.assertEqual(result, {"1301": date(2026, 5, 13), "2914": date(2026, 5, 8)})

    def test_skips_announcements_before_asof(self) -> None:
        records = [
            {"Code": "13010", "Date": "2026-04-20T00:00:00"},
            {"Code": "13010", "Date": "2026-05-13T00:00:00"},
        ]
        result = _index_next_earnings(records, date(2026, 4, 25))
        self.assertEqual(result, {"1301": date(2026, 5, 13)})

    def test_handles_invalid_codes_and_dates_without_raising(self) -> None:
        records = [
            {"Code": "", "Date": "2026-05-13"},
            {"Code": "13010"},
            {"Code": "abcde", "Date": "2026-05-13"},
        ]
        result = _index_next_earnings(records, date(2026, 4, 25))
        self.assertEqual(result, {"ABCD": date(2026, 5, 13)})


def _edinet_csv_zip(*, include_debt: bool = True) -> bytes:
    rows = [
        ("jpcrp_cor:NetSales", "CurrentYearDuration_ConsolidatedMember", "1000"),
        (
            "jpcrp_cor:NetCashProvidedByUsedInOperatingActivities",
            "CurrentYearDuration_ConsolidatedMember",
            "900",
        ),
        ("jpcrp_cor:OperatingProfit", "CurrentYearDuration_ConsolidatedMember", "150"),
        ("jpcrp_cor:CashAndDeposits", "CurrentYearInstant_ConsolidatedMember", "1000"),
        (
            "jpcrp_cor:PurchaseOfPropertyPlantAndEquipment",
            "CurrentYearDuration_ConsolidatedMember",
            "-200",
        ),
        ("jpcrp_cor:Equity", "CurrentYearInstant_ConsolidatedMember", "1200"),
        ("jpcrp_cor:TotalAssets", "CurrentYearInstant_ConsolidatedMember", "2000"),
        ("jpcrp_cor:DepreciationAndAmortization", "CurrentYearDuration_ConsolidatedMember", "50"),
    ]
    if include_debt:
        rows.extend(
            [
                ("jpcrp_cor:ShortTermBorrowings", "CurrentYearInstant_ConsolidatedMember", "100"),
                ("jpcrp_cor:LongTermBorrowings", "CurrentYearInstant_ConsolidatedMember", "300"),
            ]
        )
    text = "要素ID\tコンテキストID\t値\n" + "\n".join("\t".join(row) for row in rows)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("XBRL_TO_CSV/test.csv", text.encode("utf-16"))
    return buffer.getvalue()


class SelectCommandTests(unittest.TestCase):
    def _write_candidates(
        self, root: Path, asof: date, candidates: list[dict[str, object]]
    ) -> Path:
        path = root / f"{asof:%Y}" / f"{asof:%m}" / f"{asof:%Y-%m-%d}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        normalized_candidates: list[dict[str, object]] = []
        for index, candidate in enumerate(candidates):
            item = dict(candidate)
            item.setdefault("market_cap_oku", 300)
            item.setdefault("avg_turnover_oku", 2.0)
            item.setdefault("listing_span_days", 1200)
            item.setdefault("jpx_flags", [])
            metrics = dict(cast(dict[str, object], item.get("metrics") or {}))
            metrics.setdefault("er_annual", round(1.0 - index * 0.001, 6))
            item["metrics"] = metrics
            normalized_candidates.append(item)
        payload = yaml.safe_dump(
            {"candidates": normalized_candidates}, allow_unicode=True, sort_keys=False
        )
        path.write_text(payload, encoding="utf-8")
        return path

    def _write_macro_context(self, root: Path, asof: date, sectors: dict[str, str | None]) -> Path:
        path = root / f"{asof:%Y}" / f"{asof:%m}" / f"macro-context-{asof:%Y-%m-%d}-test.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        del sectors
        path.write_text(
            yaml.safe_dump(
                {
                    "schema_version": 2,
                    "kind": "macro-context",
                    "context_id": f"macro-context-{asof:%Y-%m-%d}-test",
                    "as_of": asof.isoformat(),
                    "valid_until": (asof + timedelta(days=7)).isoformat(),
                    "published_at": f"{asof.isoformat()}T00:00:00+09:00",
                    "summary": "test",
                    "inputs": {
                        "articles": [
                            {
                                "input_id": "article-test",
                                "source": "test",
                                "title": "test",
                                "url": "https://example.com/macro",
                                "published_at": f"{asof.isoformat()}T00:00:00+09:00",
                                "accessed_at": f"{asof.isoformat()}T00:00:00+09:00",
                                "status": "ok",
                                "used_for": "test delta",
                            }
                        ],
                        "indicator_series": [],
                    },
                    "material_deltas": [
                        {
                            "channel": "common_tail",
                            "direction": "mixed",
                            "materiality": "low",
                            "summary": "test delta",
                            "used_for": "test context",
                            "source_ids": ["article-test"],
                        }
                    ],
                    "sizing_cautions": [],
                    "research_questions": ["test question"],
                    "refresh_triggers": ["test trigger"],
                    "changes_since_previous": [],
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return path

    def _recommended(self, payload: dict[str, object]) -> list[dict[str, object]]:
        recommended = payload["recommendations"]
        self.assertIsInstance(recommended, list)
        return cast(list[dict[str, object]], recommended)

    def test_er_annual_is_primary_ranking_key(self) -> None:
        """E[r] を持つ候補は playbook 優先順より前に、E[r] 降順で並ぶ。

        cash-rich (playbook 最優先) だが E[r] の低い 1111 より、
        valuation-reversion で E[r] の高い 2222 が先頭に来る。E[r] 欠損の
        3333 は ranking 母集団から外れる (#309)。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            self._write_candidates(
                root / "records/02-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "top playbook low er",
                        "sector_33": "電気機器",
                        "market_cap_oku": 2000,
                        "metrics": {"er_annual": 0.02},
                        "evidence_hits": [{"name": "cash-rich-asset-discount"}],
                    },
                    {
                        "ticker": "2222",
                        "name": "high er",
                        "sector_33": "機械",
                        "market_cap_oku": 600,
                        "metrics": {"er_annual": 0.09},
                        "evidence_hits": [{"name": "valuation-reversion"}],
                    },
                    {
                        "ticker": "3333",
                        "name": "er missing",
                        "sector_33": "化学",
                        "market_cap_oku": 400,
                        "metrics": {"er_annual": None},
                        "evidence_hits": [{"name": "cash-rich-asset-discount"}],
                    },
                ],
            )
            self._write_macro_context(
                root / "records/01-macro-context",
                asof,
                sectors={"機械": "neutral"},
            )
            buffer = io.StringIO()
            exit_code = select_command(
                asof_date=asof,
                macro_context_path=None,
                top=10,
                candidates_root=root / "records/02-candidates",
                macro_context_root=root / "records/01-macro-context",
                stdout=buffer,
            )
            self.assertEqual(exit_code, 0)
            payload = safe_load(buffer.getvalue())
            tickers = [c["ticker"] for c in self._recommended(payload)]
            self.assertEqual(tickers, ["2222", "1111"])
            self.assertEqual(payload["selection"]["counts"]["er_missing"], 1)

    def test_applies_macro_context_without_dropping_headwind_sectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            self._write_candidates(
                root / "records/02-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "headwind exclude",
                        "sector_33": "石油・石炭製品",
                        "market_cap_oku": 2000,
                        "evidence_hits": [{"name": "valuation-reversion"}],
                    },
                    {
                        "ticker": "2222",
                        "name": "single hit",
                        "sector_33": "電気機器",
                        "market_cap_oku": 600,
                        "evidence_hits": [{"name": "valuation-reversion"}],
                    },
                    {
                        "ticker": "3333",
                        "name": "triple hit",
                        "sector_33": "機械",
                        "market_cap_oku": 400,
                        "evidence_hits": [
                            {"name": "valuation-reversion"},
                            {"name": "cashflow-yield-discount"},
                            {"name": "cashflow-yield-discount"},
                        ],
                    },
                ],
            )
            self._write_macro_context(
                root / "records/01-macro-context",
                asof,
                sectors={
                    "石油・石炭製品": "headwind",
                    "電気機器": "neutral",
                    "機械": "tailwind",
                },
            )
            buffer = io.StringIO()
            exit_code = select_command(
                asof_date=asof,
                macro_context_path=None,
                top=10,
                candidates_root=root / "records/02-candidates",
                macro_context_root=root / "records/01-macro-context",
                stdout=buffer,
            )
            self.assertEqual(exit_code, 0)
            payload = safe_load(buffer.getvalue())
            self.assertNotIn("input_count", payload)
            self.assertEqual(payload["selection"]["counts"]["input"], 3)
            self.assertEqual(
                payload["selection"]["macro_context_summary"]["research_questions"],
                ["test question"],
            )
            self.assertEqual(
                payload["selection"]["macro_context_summary"]["refresh_triggers"],
                ["test trigger"],
            )
            tickers = [c["ticker"] for c in self._recommended(payload)]
            # macro context はcontext-level annotationで順位に影響しない。helper が
            # 付ける er_annual 既定値の降順で並ぶ。
            self.assertEqual(tickers, ["1111", "2222", "3333"])
            self.assertEqual(
                payload["selection"]["research_selection_playbook_order"],
                [
                    "cash-rich-asset-discount",
                    "valuation-reversion",
                    "cashflow-yield-discount",
                    "sales-discount-growth",
                ],
            )
            self.assertEqual(
                self._recommended(payload)[0]["selection_playbook"], "valuation-reversion"
            )
            self.assertEqual(self._recommended(payload)[0]["position_tier"], "1000+")
            self.assertNotIn("lenses", self._recommended(payload)[0])

            full_buffer = io.StringIO()
            full_exit_code = select_command(
                asof_date=asof,
                macro_context_path=None,
                top=10,
                candidates_root=root / "records/02-candidates",
                macro_context_root=root / "records/01-macro-context",
                detail="full",
                stdout=full_buffer,
            )
            self.assertEqual(full_exit_code, 0)
            full_payload = safe_load(full_buffer.getvalue())
            self.assertEqual(full_payload["selection"]["detail"], "full")
            self.assertIn("lenses", self._recommended(full_payload)[0])
            self.assertEqual(self._recommended(payload)[1]["position_tier"], "500-1000")

    def test_select_command_accepts_explicit_candidates_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            canonical = self._write_candidates(
                root / "records/02-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "custom path",
                        "sector_33": "機械",
                        "market_cap_oku": 600,
                        "evidence_hits": [{"name": "valuation-reversion"}],
                    }
                ],
            )
            custom = root / "records/02-candidates/e2e/custom-candidates.yaml"
            custom.parent.mkdir(parents=True)
            custom.write_text(canonical.read_text(encoding="utf-8"), encoding="utf-8")
            canonical.unlink()
            self._write_macro_context(
                root / "records/01-macro-context", asof, sectors={"機械": "tailwind"}
            )

            buffer = io.StringIO()
            exit_code = select_command(
                asof_date=asof,
                macro_context_path=None,
                candidates_path=custom,
                top=10,
                macro_context_root=root / "records/01-macro-context",
                stdout=buffer,
            )

            self.assertEqual(exit_code, 0)
            payload = safe_load(buffer.getvalue())
            self.assertEqual(
                payload["selection"]["input_refs"]["candidates_ref"],
                "records/02-candidates/e2e/custom-candidates.yaml",
            )
            self.assertEqual([item["ticker"] for item in self._recommended(payload)], ["1111"])

    def test_select_accepts_price_history_continuity_fields(self) -> None:
        """run が出力する新しい連続性 fact を select 側 loader が受理する回帰確認。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            self._write_candidates(
                root / "records/02-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "gappy history",
                        "sector_33": "機械",
                        "market_cap_oku": 600,
                        "listing_span_days": 1200,
                        "price_history_sessions_750d": 130,
                        "price_history_coverage_750d": 0.27,
                        "evidence_hits": [{"name": "valuation-reversion"}],
                    }
                ],
            )
            self._write_macro_context(
                root / "records/01-macro-context", asof, sectors={"機械": "tailwind"}
            )

            buffer = io.StringIO()
            exit_code = select_command(
                asof_date=asof,
                macro_context_path=None,
                candidates_path=None,
                top=10,
                candidates_root=root / "records/02-candidates",
                macro_context_root=root / "records/01-macro-context",
                stdout=buffer,
            )

            self.assertEqual(exit_code, 0)
            payload = safe_load(buffer.getvalue())
            recommended = self._recommended(payload)
            self.assertEqual(recommended[0]["ticker"], "1111")
            self.assertEqual(recommended[0]["price_history_coverage_750d"], 0.27)
            self.assertIn("price_history_gap", recommended[0]["risk_tags"])

    def test_select_recommends_by_simple_rank_and_diversity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            self._write_candidates(
                root / "records/02-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "valuation reversion leads the configured playbook order",
                        "sector_33": "機械",
                        "market_cap_oku": 600,
                        "evidence_hits": [{"name": "valuation-reversion"}],
                    },
                    {
                        "ticker": "2222",
                        "name": "strict net cash ranks second in playbook order",
                        "sector_33": "機械",
                        "market_cap_oku": 150,
                        "evidence_hits": [
                            {
                                "name": "valuation-reversion",
                                "metrics": {
                                    "net_cash_to_market_cap": 0.6,
                                    "price_to_equity": 0.7,
                                },
                            }
                        ],
                    },
                ],
            )
            self._write_macro_context(
                root / "records/01-macro-context",
                asof,
                sectors={"機械": "neutral"},
            )

            buffer = io.StringIO()
            exit_code = select_command(
                asof_date=asof,
                macro_context_path=None,
                top=10,
                candidates_root=root / "records/02-candidates",
                macro_context_root=root / "records/01-macro-context",
                detail="full",
                stdout=buffer,
            )

            self.assertEqual(exit_code, 0)
            payload = safe_load(buffer.getvalue())
            # research_selection_playbook_order is the single playbook priority: the
            # valuation-reversion candidate outranks the alternative-playbook peers.
            # Sector cap=2 keeps both 機械 names through to recommended.
            self.assertEqual([c["ticker"] for c in self._recommended(payload)], ["1111", "2222"])
            self.assertEqual(
                self._recommended(payload)[0]["selection_playbook"], "valuation-reversion"
            )

    def test_select_preserves_freshness_warnings_across_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            warning = {
                "source_family": "edinet-metrics",
                "stale_metric": "edinet_metrics",
                "reason": "material_event_after_edinet_source",
                "event_date": "2026-03-03",
                "event_kind": "borrowing",
                "event_title": "資金の借入に関するお知らせ",
                "event_source": "tdnet",
                "event_url": None,
                "edinet_source_submit_datetime": "2026-03-03 10:00",
            }
            self._write_candidates(
                root / "records/02-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "2222",
                        "name": "strict net cash warning",
                        "sector_33": "機械",
                        "market_cap_oku": 150,
                        "freshness_warnings": [warning],
                        "evidence_hits": [
                            {
                                "name": "valuation-reversion",
                                "metrics": {
                                    "net_cash_to_market_cap": 0.6,
                                    "price_to_equity": 0.7,
                                },
                            }
                        ],
                    },
                ],
            )
            self._write_macro_context(
                root / "records/01-macro-context",
                asof,
                sectors={"機械": "neutral"},
            )

            buffer = io.StringIO()
            exit_code = select_command(
                asof_date=asof,
                macro_context_path=None,
                top=10,
                candidates_root=root / "records/02-candidates",
                macro_context_root=root / "records/01-macro-context",
                detail="full",
                stdout=buffer,
            )

            self.assertEqual(exit_code, 0)
            payload = safe_load(buffer.getvalue())
            self.assertEqual(self._recommended(payload)[0]["freshness_warnings"], [warning])

    def test_select_keeps_er_ranked_candidate_without_sizing_eligible_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            self._write_candidates(
                root / "records/02-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "warning-only false positive",
                        "sector_33": "機械",
                        "market_cap_oku": 150,
                        "evidence_hits": [
                            {
                                "name": "valuation-reversion",
                                "source_status": "warning",
                                "sizing_eligible": False,
                                "metrics": {
                                    "net_cash_to_market_cap": 0.9,
                                    "price_to_equity": 0.5,
                                },
                            }
                        ],
                    },
                    {
                        "ticker": "2222",
                        "name": "eligible bargain",
                        "sector_33": "機械",
                        "market_cap_oku": 180,
                        "evidence_hits": [
                            {
                                "name": "valuation-reversion",
                                "source_status": "ok",
                                "sizing_eligible": True,
                                "metrics": {
                                    "net_cash_to_market_cap": 0.5,
                                    "price_to_equity": 0.8,
                                },
                            }
                        ],
                    },
                ],
            )
            self._write_macro_context(
                root / "records/01-macro-context",
                asof,
                sectors={"機械": "neutral"},
            )

            buffer = io.StringIO()
            exit_code = select_command(
                asof_date=asof,
                macro_context_path=None,
                top=10,
                candidates_root=root / "records/02-candidates",
                macro_context_root=root / "records/01-macro-context",
                detail="full",
                stdout=buffer,
            )

            self.assertEqual(exit_code, 0)
            payload = safe_load(buffer.getvalue())
            recommended = self._recommended(payload)
            self.assertEqual([c["ticker"] for c in recommended], ["1111", "2222"])
            self.assertIsNone(recommended[0]["selection_playbook"])
            self.assertEqual(recommended[1]["selection_playbook"], "valuation-reversion")

    def test_select_handles_multi_playbook_candidates_with_primary_selection_playbook(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            self._write_candidates(
                root / "records/02-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "strict top",
                        "sector_33": "機械",
                        "market_cap_oku": 300,
                        "evidence_hits": [
                            {
                                "name": "valuation-reversion",
                                "metrics": {
                                    "net_cash_to_market_cap": 0.9,
                                    "price_to_equity": 0.6,
                                },
                            }
                        ],
                    },
                    {
                        "ticker": "2222",
                        "name": "strict and sales",
                        "sector_33": "電気機器",
                        "market_cap_oku": 300,
                        "evidence_hits": [
                            {
                                "name": "valuation-reversion",
                                "metrics": {
                                    "net_cash_to_market_cap": 0.4,
                                    "price_to_equity": 0.8,
                                },
                            },
                            {
                                "name": "sales-discount-growth",
                                "metrics": {
                                    "ps_sector_gap": -0.7,
                                    "sales_yoy": 0.12,
                                    "operating_profit": 10.0,
                                },
                            },
                        ],
                    },
                ],
            )
            self._write_macro_context(
                root / "records/01-macro-context",
                asof,
                sectors={"機械": "neutral", "電気機器": "neutral"},
            )

            buffer = io.StringIO()
            exit_code = select_command(
                asof_date=asof,
                macro_context_path=None,
                top=10,
                candidates_root=root / "records/02-candidates",
                macro_context_root=root / "records/01-macro-context",
                detail="full",
                stdout=buffer,
            )

            self.assertEqual(exit_code, 0)
            payload = safe_load(buffer.getvalue())
            self.assertEqual([c["ticker"] for c in self._recommended(payload)], ["1111", "2222"])
            self.assertEqual(
                self._recommended(payload)[1]["selection_playbook"], "valuation-reversion"
            )

    def test_select_uses_playbook_strength_before_market_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            self._write_candidates(
                root / "records/02-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "large weak cashflow",
                        "sector_33": "機械",
                        "market_cap_oku": 5000,
                        "metrics": {"er_annual": 0.05},
                        "evidence_hits": [
                            {
                                "name": "cashflow-yield-discount",
                                "metrics": {
                                    "ocf_yield": 0.10,
                                    "cfo_yoy": -0.05,
                                },
                            }
                        ],
                    },
                    {
                        "ticker": "2222",
                        "name": "small strong cashflow",
                        "sector_33": "電気機器",
                        "market_cap_oku": 150,
                        "metrics": {"er_annual": 0.05},
                        "evidence_hits": [
                            {
                                "name": "cashflow-yield-discount",
                                "metrics": {
                                    "ocf_yield": 0.18,
                                    "cfo_yoy": 0.2,
                                },
                            }
                        ],
                    },
                ],
            )
            self._write_macro_context(
                root / "records/01-macro-context",
                asof,
                sectors={"機械": "neutral", "電気機器": "neutral"},
            )
            buffer = io.StringIO()
            exit_code = select_command(
                asof_date=asof,
                macro_context_path=None,
                top=10,
                candidates_root=root / "records/02-candidates",
                macro_context_root=root / "records/01-macro-context",
                detail="full",
                stdout=buffer,
            )
            self.assertEqual(exit_code, 0)
            payload = safe_load(buffer.getvalue())
            self.assertEqual([c["ticker"] for c in self._recommended(payload)], ["2222", "1111"])
            self.assertEqual(
                self._recommended(payload)[0]["selection_playbook"], "cashflow-yield-discount"
            )

    def test_select_annotates_durability_and_flags_invalid_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            self._write_candidates(
                root / "records/02-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "price-only drop",
                        "sector_33": "機械",
                        "market_cap_oku": 300,
                        "price_change_5d": -0.12,
                        "evidence_hits": [{"name": "cashflow-yield-discount"}],
                    },
                    {
                        "ticker": "2222",
                        "name": "well guarded drop",
                        "sector_33": "機械",
                        "market_cap_oku": 300,
                        "avg_turnover_oku": 1.5,
                        "price_change_5d": -0.09,
                        "turnover_spike_5d": 1.0,
                        "metrics": {
                            "equity_ratio": 0.5,
                            "price_to_equity": 0.9,
                            "ocf_yield": 0.12,
                            "fcf_yield": 0.06,
                            "net_cash_to_market_cap": 0.25,
                            "sales_yoy": 0.1,
                            "operating_profit": 10.0,
                        },
                        "evidence_hits": [{"name": "cashflow-yield-discount"}],
                    },
                    {
                        "ticker": "3333",
                        "name": "deeper drop with thin guard",
                        "sector_33": "機械",
                        "market_cap_oku": 300,
                        "price_change_5d": -0.2,
                        "metrics": {"ocf_yield": 0.12},
                        "evidence_hits": [{"name": "cashflow-yield-discount"}],
                    },
                    {
                        "ticker": "4444",
                        "name": "turnover-only spike",
                        "sector_33": "機械",
                        "market_cap_oku": 300,
                        "turnover_spike_5d": 3.0,
                        "metrics": {
                            "ocf_yield": 0.12,
                            "fcf_yield": 0.06,
                            "operating_profit": 10.0,
                        },
                        "evidence_hits": [{"name": "cashflow-yield-discount"}],
                    },
                    {
                        "ticker": "4445",
                        "name": "cash-flow family only",
                        "sector_33": "機械",
                        "market_cap_oku": 300,
                        "price_change_5d": -0.2,
                        "metrics": {
                            "ocf_yield": 0.12,
                            "fcf_yield": 0.06,
                            "operating_profit": -10.0,
                        },
                        "evidence_hits": [{"name": "cashflow-yield-discount"}],
                    },
                    {
                        "ticker": "5555",
                        "name": "invalid numeric metrics",
                        "sector_33": "機械",
                        "market_cap_oku": 300,
                        "price_change_5d": -0.2,
                        "metrics": {
                            "ocf_yield": "0.12",
                            "operating_profit": True,
                            "sales_yoy": "0.2",
                        },
                        "evidence_hits": [
                            {
                                "name": "cashflow-yield-discount",
                                "metrics": {"ocf_yield": "0.12"},
                            }
                        ],
                    },
                ],
            )
            self._write_macro_context(
                root / "records/01-macro-context", asof, sectors={"機械": "neutral"}
            )

            buffer = io.StringIO()
            exit_code = select_command(
                asof_date=asof,
                macro_context_path=None,
                top=10,
                candidates_root=root / "records/02-candidates",
                macro_context_root=root / "records/01-macro-context",
                detail="full",
                stdout=buffer,
            )

            self.assertEqual(exit_code, 0)
            payload = safe_load(buffer.getvalue())
            by_ticker = {item["ticker"]: item for item in self._recommended(payload)}
            self.assertIn(
                "liquidity_pass",
                by_ticker["2222"]["lenses"]["durability"]["reasons"],
            )
            diagnostics = payload["selection"]["diagnostics"]
            self.assertIn("invalid_numeric_metric_values", diagnostics["warnings"])
            self.assertEqual(diagnostics["invalid_numeric_metric_value_count"], 4)
            self.assertEqual(sum(diagnostics["durability_counts"].values()), 6)

    def test_select_durability_lens_splits_missing_and_weak_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            self._write_candidates(
                root / "records/02-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "missing long hold inputs",
                        "sector_33": "機械",
                        "market_cap_oku": 300,
                        "evidence_hits": [{"name": "cashflow-yield-discount"}],
                    },
                    {
                        "ticker": "2222",
                        "name": "weak long hold inputs",
                        "sector_33": "電気機器",
                        "market_cap_oku": 300,
                        "avg_turnover_oku": 1.2,
                        "metrics": {
                            "equity_ratio": 0.1,
                            "ocf_yield": -0.01,
                            "operating_profit": -1.0,
                        },
                        "evidence_hits": [{"name": "cashflow-yield-discount"}],
                    },
                ],
            )
            self._write_macro_context(
                root / "records/01-macro-context",
                asof,
                sectors={"機械": "neutral", "電気機器": "neutral"},
            )

            buffer = io.StringIO()
            exit_code = select_command(
                asof_date=asof,
                macro_context_path=None,
                top=10,
                candidates_root=root / "records/02-candidates",
                macro_context_root=root / "records/01-macro-context",
                detail="full",
                stdout=buffer,
            )

            self.assertEqual(exit_code, 0)
            payload = safe_load(buffer.getvalue())
            by_ticker = {item["ticker"]: item for item in self._recommended(payload)}
            missing_lens = by_ticker["1111"]["lenses"]["durability"]
            weak_lens = by_ticker["2222"]["lenses"]["durability"]
            self.assertEqual(missing_lens["rating"], "low")
            self.assertIn("equity_ratio_missing", missing_lens["missing_reasons"])
            self.assertEqual(missing_lens["weak_reasons"], [])
            self.assertEqual(weak_lens["rating"], "low")
            self.assertIn("low_equity_ratio", weak_lens["weak_reasons"])
            self.assertEqual(weak_lens["missing_reasons"], [])

    def test_select_does_not_cap_previous_candidates(self) -> None:
        """E[r] 主キーでは割安上位の月またぎ持続が正常な挙動なので、
        previous-candidate cap は掛けない (較正リプレイの計測構成と一致)。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            previous_asof = date(2026, 4, 17)
            previous_tickers = ("1111", "2222", "3333", "4444")
            self._write_candidates(
                root / "records/02-candidates",
                previous_asof,
                candidates=[
                    {
                        "ticker": ticker,
                        "name": f"previous {ticker}",
                        "sector_33": "機械",
                        "market_cap_oku": 300,
                        "evidence_hits": [{"name": "cashflow-yield-discount"}],
                    }
                    for ticker in previous_tickers
                ],
            )
            self._write_candidates(
                root / "records/02-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "previous strict",
                        "sector_33": "機械",
                        "market_cap_oku": 300,
                        "evidence_hits": [{"name": "valuation-reversion"}],
                    },
                    {
                        "ticker": "2222",
                        "name": "previous fcf",
                        "sector_33": "電気機器",
                        "market_cap_oku": 300,
                        "evidence_hits": [{"name": "cashflow-yield-discount"}],
                    },
                    {
                        "ticker": "3333",
                        "name": "previous cash rich",
                        "sector_33": "小売業",
                        "market_cap_oku": 300,
                        "evidence_hits": [{"name": "cashflow-yield-discount"}],
                    },
                    {
                        "ticker": "4444",
                        "name": "previous cashflow",
                        "sector_33": "サービス業",
                        "market_cap_oku": 300,
                        "evidence_hits": [{"name": "cashflow-yield-discount"}],
                    },
                    {
                        "ticker": "5555",
                        "name": "new sales",
                        "sector_33": "医薬品",
                        "market_cap_oku": 300,
                        "evidence_hits": [{"name": "sales-discount-growth"}],
                    },
                ],
            )
            self._write_macro_context(
                root / "records/01-macro-context",
                asof,
                sectors={
                    "機械": "neutral",
                    "電気機器": "neutral",
                    "小売業": "neutral",
                    "サービス業": "neutral",
                    "医薬品": "neutral",
                },
            )

            buffer = io.StringIO()
            exit_code = select_command(
                asof_date=asof,
                macro_context_path=None,
                top=10,
                candidates_root=root / "records/02-candidates",
                macro_context_root=root / "records/01-macro-context",
                stdout=buffer,
            )

            self.assertEqual(exit_code, 0)
            payload = safe_load(buffer.getvalue())
            tickers = [item["ticker"] for item in self._recommended(payload)]
            self.assertIn("5555", tickers)
            # 繰越候補は cap されず全員通る (新規 5555 も並存)。
            self.assertEqual(
                sum(1 for item in self._recommended(payload) if item["previous_candidate"]),
                4,
            )

    def test_select_passes_repeated_previous_candidates_through(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            previous_asof = date(2026, 4, 17)
            candidates = [
                {
                    "ticker": ticker,
                    "name": f"previous {ticker}",
                    "sector_33": sector,
                    "market_cap_oku": 300,
                    "evidence_hits": [{"name": "cashflow-yield-discount"}],
                }
                for ticker, sector in (
                    ("1111", "機械"),
                    ("2222", "電気機器"),
                    ("3333", "小売業"),
                )
            ]
            self._write_candidates(root / "records/02-candidates", previous_asof, candidates)
            self._write_candidates(root / "records/02-candidates", asof, candidates)
            self._write_macro_context(
                root / "records/01-macro-context",
                asof,
                sectors={"機械": "neutral", "電気機器": "neutral", "小売業": "neutral"},
            )

            buffer = io.StringIO()
            exit_code = select_command(
                asof_date=asof,
                macro_context_path=None,
                top=10,
                candidates_root=root / "records/02-candidates",
                macro_context_root=root / "records/01-macro-context",
                stdout=buffer,
            )

            self.assertEqual(exit_code, 0)
            payload = safe_load(buffer.getvalue())
            self.assertEqual(len(self._recommended(payload)), 3)
            self.assertEqual(
                sum(1 for item in self._recommended(payload) if item["previous_candidate"]),
                3,
            )

    def test_rejects_non_mapping_candidates_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            candidates_path = (
                root
                / "records/02-candidates"
                / f"{asof:%Y}"
                / f"{asof:%m}"
                / f"{asof:%Y-%m-%d}.yaml"
            )
            candidates_path.parent.mkdir(parents=True, exist_ok=True)
            # YAML root is a list rather than a mapping; should fail-fast.
            candidates_path.write_text(
                yaml.safe_dump([{"ticker": "1111"}], allow_unicode=True),
                encoding="utf-8",
            )
            self._write_macro_context(root / "records/01-macro-context", asof, sectors={})

            buffer = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = select_command(
                    asof_date=asof,
                    macro_context_path=None,
                    top=10,
                    candidates_root=root / "records/02-candidates",
                    macro_context_root=root / "records/01-macro-context",
                    stdout=buffer,
                )
            self.assertEqual(exit_code, 1)
            self.assertIn("invalid candidates YAML", stderr.getvalue())
            self.assertIn("must be a mapping", stderr.getvalue())

    def test_select_rejects_future_macro_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            self._write_candidates(
                root / "records/02-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "candidate",
                        "sector_33": "機械",
                        "market_cap_oku": 300,
                        "evidence_hits": [{"name": "cashflow-yield-discount"}],
                    }
                ],
            )
            self._write_macro_context(
                root / "records/01-macro-context",
                date(2026, 5, 1),
                sectors={"機械": "neutral"},
            )

            buffer = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = select_command(
                    asof_date=asof,
                    macro_context_path=(
                        root / "records/01-macro-context/2026/05/macro-context-2026-05-01-test.yaml"
                    ),
                    top=10,
                    candidates_root=root / "records/02-candidates",
                    macro_context_root=root / "records/01-macro-context",
                    stdout=buffer,
                )

            self.assertEqual(exit_code, 1)
            self.assertIn("macro context as_of is after screening asof", stderr.getvalue())

    def test_select_rejects_legacy_macro_context_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            self._write_candidates(
                root / "records/02-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "candidate",
                        "sector_33": "機械",
                        "market_cap_oku": 300,
                        "evidence_hits": [{"name": "cashflow-yield-discount"}],
                    }
                ],
            )
            context_path = self._write_macro_context(
                root / "records/01-macro-context", asof, sectors={}
            )
            context = safe_load(context_path.read_text(encoding="utf-8"))
            assert isinstance(context, dict)
            context["schema_version"] = 1
            context_path.write_text(
                yaml.safe_dump(context, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = select_command(
                    asof_date=asof,
                    macro_context_path=context_path,
                    top=10,
                    candidates_root=root / "records/02-candidates",
                    macro_context_root=root / "records/01-macro-context",
                    stdout=io.StringIO(),
                )

            self.assertEqual(exit_code, 1)
            self.assertIn("schema invalid at schema_version", stderr.getvalue())

    def test_select_rejects_macro_context_with_invalid_validity_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            self._write_candidates(
                root / "records/02-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "candidate",
                        "sector_33": "機械",
                        "market_cap_oku": 300,
                        "evidence_hits": [{"name": "cashflow-yield-discount"}],
                    }
                ],
            )
            context_path = self._write_macro_context(
                root / "records/01-macro-context", asof, sectors={}
            )
            context = safe_load(context_path.read_text(encoding="utf-8"))
            assert isinstance(context, dict)
            context["valid_until"] = "2026-04-23"
            context_path.write_text(
                yaml.safe_dump(context, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = select_command(
                    asof_date=asof,
                    macro_context_path=context_path,
                    top=10,
                    candidates_root=root / "records/02-candidates",
                    macro_context_root=root / "records/01-macro-context",
                    stdout=io.StringIO(),
                )

            self.assertEqual(exit_code, 1)
            self.assertIn("valid_until must not predate as_of", stderr.getvalue())

    def test_select_rejects_macro_context_with_unresolved_delta_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            self._write_candidates(
                root / "records/02-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "candidate",
                        "sector_33": "機械",
                        "market_cap_oku": 300,
                        "evidence_hits": [{"name": "cashflow-yield-discount"}],
                    }
                ],
            )
            context_path = self._write_macro_context(
                root / "records/01-macro-context", asof, sectors={}
            )
            context = safe_load(context_path.read_text(encoding="utf-8"))
            assert isinstance(context, dict)
            deltas = context["material_deltas"]
            assert isinstance(deltas, list)
            delta = deltas[0]
            assert isinstance(delta, dict)
            delta["source_ids"] = ["missing-source"]
            context_path.write_text(
                yaml.safe_dump(context, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = select_command(
                    asof_date=asof,
                    macro_context_path=context_path,
                    top=10,
                    candidates_root=root / "records/02-candidates",
                    macro_context_root=root / "records/01-macro-context",
                    stdout=io.StringIO(),
                )

            self.assertEqual(exit_code, 1)
            self.assertIn("references unknown input IDs", stderr.getvalue())

    def test_select_rejects_malformed_macro_context_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            self._write_candidates(
                root / "records/02-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "candidate",
                        "sector_33": "機械",
                        "market_cap_oku": 300,
                        "evidence_hits": [{"name": "cashflow-yield-discount"}],
                    }
                ],
            )
            context_path = root / "invalid.yaml"
            context_path.write_text("kind: [macro-context\n", encoding="utf-8")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = select_command(
                    asof_date=asof,
                    macro_context_path=context_path,
                    top=10,
                    candidates_root=root / "records/02-candidates",
                    macro_context_root=root / "records/01-macro-context",
                    stdout=io.StringIO(),
                )

            self.assertEqual(exit_code, 1)
            self.assertIn("failed to load macro context", stderr.getvalue())

    def test_select_allows_stale_macro_context_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 5, 20)
            self._write_candidates(
                root / "records/02-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "candidate",
                        "sector_33": "機械",
                        "market_cap_oku": 300,
                        "evidence_hits": [{"name": "cashflow-yield-discount"}],
                    }
                ],
            )
            self._write_macro_context(
                root / "records/01-macro-context",
                date(2026, 5, 1),
                sectors={"機械": "neutral"},
            )

            buffer = io.StringIO()
            exit_code = select_command(
                asof_date=asof,
                macro_context_path=(
                    root / "records/01-macro-context/2026/05/macro-context-2026-05-01-test.yaml"
                ),
                top=10,
                candidates_root=root / "records/02-candidates",
                macro_context_root=root / "records/01-macro-context",
                stdout=buffer,
            )

            self.assertEqual(exit_code, 0)
            payload = safe_load(buffer.getvalue())
            self.assertIn(
                "macro_context_stale", payload["selection"]["macro_context_summary"]["warnings"]
            )

    def test_select_allows_missing_macro_context_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 5, 20)
            self._write_candidates(
                root / "records/02-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "candidate",
                        "sector_33": "機械",
                        "market_cap_oku": 300,
                        "evidence_hits": [{"name": "cashflow-yield-discount"}],
                    }
                ],
            )

            buffer = io.StringIO()
            exit_code = select_command(
                asof_date=asof,
                macro_context_path=None,
                top=10,
                candidates_root=root / "records/02-candidates",
                macro_context_root=root / "records/01-macro-context",
                stdout=buffer,
            )

            self.assertEqual(exit_code, 0)
            payload = safe_load(buffer.getvalue())
            summary = payload["selection"]["macro_context_summary"]
            self.assertIsNone(summary["context_id"])
            self.assertEqual(summary["warnings"], ["macro_context_missing"])

    def test_tolerates_unknown_candidate_fields(self) -> None:
        """新しい screen fact を loader が拒否しない(単一 parsing 経路の回帰確認)。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            self._write_candidates(
                root / "records/02-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "future fact",
                        "sector_33": "機械",
                        "market_cap_oku": 300,
                        "some_future_screen_fact": -0.09,
                        "evidence_hits": [{"name": "cashflow-yield-discount"}],
                    }
                ],
            )
            self._write_macro_context(
                root / "records/01-macro-context", asof, sectors={"機械": "neutral"}
            )

            buffer = io.StringIO()
            exit_code = select_command(
                asof_date=asof,
                macro_context_path=None,
                top=10,
                candidates_root=root / "records/02-candidates",
                macro_context_root=root / "records/01-macro-context",
                stdout=buffer,
            )

            self.assertEqual(exit_code, 0)
            payload = safe_load(buffer.getvalue())
            self.assertEqual(self._recommended(payload)[0]["ticker"], "1111")
