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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from baibai_engine.foundation.yaml_io import safe_load

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_engine.foundation.time import JST
from baibai_engine.screening import cli as screening_cli
from baibai_engine.screening.cli import (
    ProviderBundle,
    backfill_history_command,
    backfill_master_command,
    bootstrap_cache_command,
    extract_edinet_metrics_command,
    run_command,
)
from baibai_engine.screening.cli.run import _index_next_earnings
from baibai_engine.screening.config import ScreeningConfig
from baibai_engine.screening.edinet_revision import compute_extractor_revision
from baibai_engine.screening.providers import JQuantsProvider
from baibai_engine.screening.providers.edinet import (
    EdinetMetricRecord,
    EDINETProviderError,
    EDINETRateLimitError,
)
from baibai_engine.screening.providers.jpx import (
    JPXEarningsCalendarEntry,
    JPXEarningsCalendarSnapshot,
    JPXProviderError,
    JPXRegulationSnapshot,
)
from baibai_engine.screening.providers.jquants import (
    JQuantsDailyBar,
    JQuantsFinancialSummary,
    JQuantsMarketCalendarDay,
    JQuantsProviderError,
    JQuantsWeeklyMargin,
)
from baibai_engine.screening.render import build_output_path
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.schema import SecurityMaster, TTMQuality
from baibai_engine.screening.sqlite_cache import store_edinet_metrics
from baibai_engine.screening.sqlite_coverage import (
    RequiredFieldCoverage,
    RequiredFieldRepairPlan,
)
from baibai_engine.screening.sqlite_reader import read_edinet_metrics


@dataclass
class FakeJQuantsProvider:
    business_day: bool = True
    treasury_shares: float = 0.0
    calls: list[tuple[str, date | None, date | None]] = field(default_factory=list)
    revision_overlap_days: list[int] = field(default_factory=list)

    def get_mkt_calendar(self, start: date, end: date) -> list[JQuantsMarketCalendarDay]:
        self.calls.append(("get_mkt_calendar", start, end))
        return [JQuantsMarketCalendarDay(day=start, is_business_day=self.business_day)]

    def get_eq_master(self, requested_asof: date) -> list[SecurityMaster]:
        self.calls.append(("get_eq_master", requested_asof, requested_asof))
        return [
            SecurityMaster(
                code="130A",
                name="Alpha",
                market_segment="Prime",
                sector_33="情報・通信業",
                is_common_stock=True,
            )
        ]

    def get_mkt_margin_interest_week(self, week_end: date) -> list[JQuantsWeeklyMargin]:
        self.calls.append(("get_mkt_margin_interest_week", week_end, week_end))
        return [
            JQuantsWeeklyMargin(
                ticker="130A",
                week_end=week_end,
                long_vol=1000.0,
                short_vol=250.0,
                long_std_vol=800.0,
                long_neg_vol=200.0,
                short_std_vol=200.0,
                short_neg_vol=50.0,
                issue_type="2",
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

    def ensure_eq_bars_daily_range(self, start: date, end: date) -> int:
        self.calls.append(("ensure_eq_bars_daily_range", start, end))
        return len(self.get_eq_bars_daily_range(start, end))

    def get_adjustment_factor_bars_range(self, start: date, end: date) -> list[JQuantsDailyBar]:
        self.calls.append(("get_adjustment_factor_bars_range", start, end))
        return []

    def refresh_fin_summary_range(
        self,
        start: date,
        end: date,
        *,
        revision_overlap_days: int,
        repair_ranges: Sequence[tuple[date, date]] = (),
        progress: Callable[[int, int, date, date], None] | None = None,
    ) -> int:
        self.calls.append(("refresh_fin_summary_range", start, end))
        self.revision_overlap_days.append(revision_overlap_days)
        del repair_ranges, progress
        return len(self.get_fin_summary_range(start, end))

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
                equity_to_asset_ratio=0.6,
                treasury_shares=self.treasury_shares,
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
                equity_to_asset_ratio=0.6,
                treasury_shares=self.treasury_shares,
                ordinary_profit=None,
                profit=None,
            ),
        ]

    def get_fy_summary_range(self, start: date, end: date) -> list[JQuantsFinancialSummary]:
        self.calls.append(("get_fy_summary_range", start, end))
        return [
            JQuantsFinancialSummary(
                ticker="130A",
                disclosed_at=date(year, 5, 15),
                eps_ttm=eps,
                fiscal_period="FY",
                fiscal_year_end=date(year, 3, 31),
            )
            for year, eps in ((2023, 10.0), (2024, 20.0), (2025, 30.0))
        ]


@dataclass
class _FailingOnDateJQuantsProvider(FakeJQuantsProvider):
    failing_date: date | None = None

    def get_eq_master(self, requested_asof: date) -> list[SecurityMaster]:
        if requested_asof == self.failing_date:
            raise JQuantsProviderError(f"no master for {requested_asof.isoformat()}")
        return super().get_eq_master(requested_asof)


@dataclass
class FakeEDINETProvider:
    documents: list[dict[str, object]] | None = None
    zip_by_doc_id: dict[str, bytes] | None = None
    bootstrap_calls: list[tuple[date, date]] = field(default_factory=list)
    download_calls: list[str] = field(default_factory=list)

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
        self.download_calls.append(doc_id)
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
    def get_eq_master(self, requested_asof: date) -> list[SecurityMaster]:
        del requested_asof
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
    earnings_requests: list[date] = field(default_factory=list)

    def get_earnings_calendar_snapshot(self, asof_date: date) -> JPXEarningsCalendarSnapshot:
        self.earnings_requests.append(asof_date)
        return JPXEarningsCalendarSnapshot(
            entries=(
                JPXEarningsCalendarEntry(
                    ticker="130A", announcement_date=asof_date + timedelta(days=7)
                ),
            ),
            source_urls=("https://www.jpx.co.jp/test/kessan.xlsx",),
            raw_record_count=1,
            excluded_record_count=0,
            superseded_record_count=None,
        )

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
                    # raw close は 899 円。自己株 10% でも normalized PER は
                    # treasury-adjusted market cap / gross shares (=809.1 円) へ戻さない。
                    jquants=FakeJQuantsProvider(treasury_shares=40_000_000.0),
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
                        output_path=build_output_path(date(2026, 4, 24)),
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
                        "jpx-public-earnings-calendar",
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
                self.assertAlmostEqual(
                    payload["candidates"][0]["metrics"]["normalized_per_3fy"],
                    44.95,
                )
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
                output_path = Path("stores/screening/candidates/e2e/candidates.yaml")
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
                    output_path=build_output_path(date(2026, 4, 24)),
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
                    output_path=build_output_path(date(2026, 4, 24)),
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
                sqlite_path=Path("stores/market/market.sqlite"),
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
                sqlite_path=Path("stores/market/market.sqlite"),
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("DatabaseError: file is not a database", stderr.getvalue())

    def test_bootstrap_cache_command_fails_when_required_field_repair_stays_incomplete(
        self,
    ) -> None:
        asof = date(2026, 5, 8)
        start = asof - timedelta(days=730)
        coverage = RequiredFieldCoverage(
            asof=asof,
            start=start,
            population_tickers=100,
            minimum_tickers=75,
            summary_tickers=100,
            shares_outstanding_tickers=0,
            treasury_shares_tickers=0,
            equity_to_asset_ratio_tickers=0,
            market_cap_required_fields_tickers=0,
            valuation_required_fields_tickers=0,
        )
        plan = RequiredFieldRepairPlan(coverage=coverage, ranges=((start, asof),))
        stderr = io.StringIO()

        with (
            patch(
                "baibai_engine.screening.cli.cache.plan_required_field_repair",
                return_value=plan,
            ),
            patch(
                "baibai_engine.screening.cli.cache.read_required_field_coverage",
                return_value=coverage,
            ),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = bootstrap_cache_command(
                asof_date=asof,
                providers=ProviderBundle(
                    jquants=FakeJQuantsProvider(),
                    edinet=FakeEDINETProvider(),
                    jpx=FakeJPXProvider(),
                ),
                sqlite_path=Path("stores/market/market.sqlite"),
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("required-field repair remained incomplete", stderr.getvalue())

    def test_bootstrap_cache_command_asof_uses_source_specific_windows(self) -> None:
        asof = date(2026, 5, 8)
        jquants = FakeJQuantsProvider()
        edinet = FakeEDINETProvider()
        jpx = FakeJPXProvider()
        buffer = io.StringIO()

        exit_code = bootstrap_cache_command(
            asof_date=asof,
            providers=ProviderBundle(jquants=jquants, edinet=edinet, jpx=jpx),
            sqlite_path=Path("stores/market/market.sqlite"),
            stdout=buffer,
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("bootstrap-cache jquants daily_bars", buffer.getvalue())
        self.assertIn("bootstrap-cache edinet documents", buffer.getvalue())
        self.assertIn("bootstrap-cache done", buffer.getvalue())
        self.assertIn(("get_eq_master", asof, asof), jquants.calls)
        self.assertIn(("get_eq_bars_daily_range", asof - timedelta(days=1200), asof), jquants.calls)
        self.assertIn(("get_fin_summary_range", asof - timedelta(days=730), asof), jquants.calls)
        self.assertIn(
            (
                "get_adjustment_factor_bars_range",
                asof - timedelta(days=2200),
                asof,
            ),
            jquants.calls,
        )
        self.assertIn(
            ("get_fy_summary_range", asof - timedelta(days=2200), asof),
            jquants.calls,
        )
        # The calendar bootstrap fetches a forward window so the unattended daily
        # batch can read the current (and upcoming) business-day rows.
        self.assertIn(
            ("get_mkt_calendar", asof - timedelta(days=7), asof + timedelta(days=45)),
            jquants.calls,
        )
        self.assertEqual(edinet.bootstrap_calls, [(asof - timedelta(days=730), asof)])
        self.assertEqual(jpx.bootstrap_calls, [asof])

    def test_backfill_master_fetches_only_the_master_for_each_date(self) -> None:
        # 1 cohort あたり 1 request だけを使うことが、この命令の存在理由である。
        jquants = FakeJQuantsProvider()
        buffer = io.StringIO()
        dates = [date(2026, 4, 30), date(2026, 5, 29)]

        with tempfile.TemporaryDirectory() as tmpdir:
            exit_code = backfill_master_command(
                asof_dates=dates,
                providers=ProviderBundle(jquants=jquants, edinet=FakeEDINETProvider(), jpx=None),
                sqlite_path=Path(tmpdir) / "market.sqlite",
                stdout=buffer,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(jquants.calls, [("get_eq_master", day, day) for day in dates])
        self.assertIn("backfill-master start: 2 date(s)", buffer.getvalue())
        # 既存 snapshot が無いので fetch 側として報告される。
        self.assertIn("2026-04-30: fetched", buffer.getvalue())
        self.assertIn("backfill-master done", buffer.getvalue())

    def test_backfill_master_continues_past_a_failed_date_and_fails_overall(self) -> None:
        # 1 日の取得失敗で残りの cohort を諦めないが、成功したことにもしない。
        jquants = _FailingOnDateJQuantsProvider(failing_date=date(2026, 4, 30))
        buffer = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr), tempfile.TemporaryDirectory() as tmpdir:
            exit_code = backfill_master_command(
                asof_dates=[date(2026, 4, 30), date(2026, 5, 29)],
                providers=ProviderBundle(jquants=jquants, edinet=FakeEDINETProvider(), jpx=None),
                sqlite_path=Path(tmpdir) / "market.sqlite",
                stdout=buffer,
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("1 of 2 date(s) failed", stderr.getvalue())
        self.assertIn(("get_eq_master", date(2026, 5, 29), date(2026, 5, 29)), jquants.calls)
        self.assertNotIn("backfill-master done", buffer.getvalue())

    def _backfill_master_main(self, argv: list[str]) -> tuple[int, str]:
        """Run backfill-master through main() with no reachable provider.

        The guards under test all reject before any fetch, so a provider that
        raises on use proves the rejection happened for the stated reason.
        """
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
                    exit_code = screening_cli.main(["backfill-master", *argv])
            finally:
                os.chdir(cwd)
        return exit_code, stderr.getvalue()

    def test_main_backfill_master_rejects_a_month_end_range_given_backwards(self) -> None:
        # 空の grid を「対象が無い」と読ませず、引数の取り違えとして名指しする。
        exit_code, stderr = self._backfill_master_main(
            ["--month-end-from", "2025-06-30", "--month-end-to", "2024-01-31"]
        )

        self.assertEqual(exit_code, 1)
        self.assertIn("2025-06-30 is after --month-end-to 2024-01-31", stderr)

    def test_main_backfill_master_rejects_one_sided_month_end_range(self) -> None:
        exit_code, stderr = self._backfill_master_main(["--month-end-from", "2025-06-30"])

        self.assertEqual(exit_code, 1)
        self.assertIn("must be given together", stderr)

    def test_main_backfill_master_rejects_a_future_asof(self) -> None:
        # 未来日の断面は存在しない。要求日を echo する provider があれば、当日の
        # population が別日の断面として永続化されてしまう。
        future = (datetime.now(JST).date() + timedelta(days=1)).isoformat()
        exit_code, stderr = self._backfill_master_main(["--asof", future])

        self.assertEqual(exit_code, 1)
        self.assertIn("rejects future dates", stderr)
        self.assertIn(future, stderr)

    def test_main_backfill_master_rejects_a_date_the_bar_store_never_priced(self) -> None:
        # 非営業日の断面も存在しない。bar store が市場の実績を持つ唯一の証拠。
        exit_code, stderr = self._backfill_master_main(["--asof", "2026-01-01"])

        self.assertEqual(exit_code, 1)
        self.assertIn("does not show as trading days", stderr)

    def test_main_backfill_master_requires_at_least_one_date(self) -> None:
        exit_code, stderr = self._backfill_master_main([])

        self.assertEqual(exit_code, 1)
        self.assertIn("resolved no dates", stderr)

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
            self.assertIn("selected=1 reused=0 downloaded=1", buffer.getvalue())

    def test_recording_a_failure_keeps_the_snapshot_when_the_store_cannot_be_read(
        self,
    ) -> None:
        # Recording a failure deletes the day's rows, and the check that is supposed to
        # stop that reads through a path which answers "no snapshot" for a store behind
        # the current schema. The write path would then migrate the store and delete a
        # snapshot that was there all along.
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            store_edinet_metrics(
                sqlite_path,
                date(2026, 4, 24),
                [
                    {
                        "secCode": "96820",
                        "sales_ttm": 1000.0,
                        "ocf_ttm": 200.0,
                        "extractor_revision": "test",
                        "source_document_revision": "test",
                    }
                ],
                status="ok",
            )
            conn = sqlite3.connect(sqlite_path)
            try:
                conn.execute("PRAGMA user_version = 1")
                conn.commit()
            finally:
                conn.close()

            screening_cli.edinet_extract._record_edinet_extraction_failure(
                sqlite_path=sqlite_path,
                asof_date=date(2026, 4, 24),
                message="provider unavailable",
            )

            conn = sqlite3.connect(sqlite_path)
            try:
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM edinet_metrics WHERE asof_date = ?",
                    ("2026-04-24",),
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(remaining, 1)

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
            self.assertIn("quality_issues=1", buffer.getvalue())
            reuse_provider = FakeEDINETProvider(documents=list(provider.documents or []))
            reuse_buffer = io.StringIO()
            reuse_exit = extract_edinet_metrics_command(
                asof_date=date(2026, 4, 25),
                lookback_days=0,
                provider=reuse_provider,
                sqlite_path=sqlite_path,
                stdout=reuse_buffer,
            )
            self.assertEqual(reuse_exit, 0)
            self.assertEqual(reuse_provider.download_calls, [])
            self.assertIn("reused=1 downloaded=0", reuse_buffer.getvalue())
            self.assertIn("quality_issues=1", reuse_buffer.getvalue())

    def test_extract_edinet_metrics_command_quarantines_affected_ticker_and_continues(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            provider = FakeEDINETProvider(
                documents=[
                    {
                        "docID": "S100MISSING",
                        "secCode": "72030",
                        "docTypeCode": "120",
                        "docInfoEditStatus": "1",
                        "withdrawalStatus": "0",
                        "disclosureStatus": "0",
                    },
                    {
                        "docID": "S100HEALTHY",
                        "secCode": "96820",
                        "docTypeCode": "120",
                        "csvFlag": "1",
                        "xbrlFlag": "1",
                    },
                ],
                zip_by_doc_id={"S100HEALTHY": _edinet_csv_zip(include_debt=True)},
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                exit_code = extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 24),
                    lookback_days=0,
                    provider=provider,
                    sqlite_path=sqlite_path,
                    stdout=stdout,
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(provider.download_calls, ["S100HEALTHY"])
            payload = read_edinet_metrics(sqlite_path, date(2026, 4, 24))
            assert payload is not None
            self.assertIsNone(payload["7203"].sales_ttm)
            self.assertEqual(
                payload["7203"].failure_reasons,
                ("document_event_quarantined:edit:S100MISSING",),
            )
            self.assertIsNotNone(payload["9682"].sales_ttm)
            self.assertIn("events=1 affected_tickers=1", stderr.getvalue())
            self.assertIn("S100MISSING(edit,type=120,ticker=7203)", stderr.getvalue())
            self.assertIn("quarantined_events=1 quarantined_tickers=1", stdout.getvalue())
            self.assertIn("quarantine_sample=S100MISSING:edit:120", stdout.getvalue())

    def test_extract_edinet_metrics_command_uses_baseline_to_quarantine_withdrawal_ticker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            store_edinet_metrics(
                sqlite_path,
                date(2026, 4, 23),
                [
                    {
                        "ticker": "7203",
                        "sales_ttm": 1000.0,
                        "source_doc_id": "S100ORIGIN",
                        "document_type": "120",
                        "extractor_revision": "seed",
                        "source_document_revision": "seed",
                    }
                ],
            )
            documents = [
                {
                    "docID": "S100WITHDRAW",
                    "parentDocID": "S100ORIGIN",
                    "docInfoEditStatus": "0",
                    "withdrawalStatus": "1",
                    "disclosureStatus": "0",
                    "legalStatus": "0",
                },
                {
                    "docID": "S100HEALTHY",
                    "secCode": "96820",
                    "docTypeCode": "120",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                },
            ]
            first_provider = FakeEDINETProvider(
                documents=documents,
                zip_by_doc_id={"S100HEALTHY": _edinet_csv_zip(include_debt=True)},
            )

            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 24),
                    lookback_days=0,
                    provider=first_provider,
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                ),
                0,
            )
            first = read_edinet_metrics(sqlite_path, date(2026, 4, 24))
            assert first is not None
            self.assertEqual(first["7203"].source_doc_id, "S100ORIGIN")
            self.assertEqual(first["7203"].document_type, "120")
            self.assertIsNone(first["7203"].sales_ttm)

            second_provider = FakeEDINETProvider(documents=documents)
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 25),
                    lookback_days=0,
                    provider=second_provider,
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                ),
                0,
            )
            second = read_edinet_metrics(sqlite_path, date(2026, 4, 25))
            assert second is not None
            self.assertIsNone(second["7203"].sales_ttm)
            self.assertEqual(
                second["7203"].failure_reasons,
                ("document_event_quarantined:withdrawal:S100WITHDRAW",),
            )
            self.assertEqual(second_provider.download_calls, [])

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
                [
                    {
                        "ticker": "9682",
                        "sales_ttm": 1_000.0,
                        "extractor_revision": "a" * 64,
                    }
                ],
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
            self.assertIsNotNone(read_edinet_metrics(sqlite_path, asof))
            row = (
                sqlite3.connect(sqlite_path)
                .execute(
                    "SELECT status, error FROM source_coverage WHERE source = ? "
                    "AND coverage_key = ?",
                    ("edinet_metrics", asof.isoformat()),
                )
                .fetchone()
            )
            self.assertEqual(row, ("ok", None))

    def test_extract_edinet_metrics_command_fails_on_invalid_document_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            asof = date(2026, 4, 24)
            store_edinet_metrics(
                sqlite_path,
                asof,
                [
                    {
                        "ticker": "9682",
                        "sales_ttm": 1_000.0,
                        "extractor_revision": "a" * 64,
                    }
                ],
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
            self.assertIsNotNone(read_edinet_metrics(sqlite_path, asof))
            row = (
                sqlite3.connect(sqlite_path)
                .execute(
                    "SELECT status, error FROM source_coverage WHERE source = ? "
                    "AND coverage_key = ?",
                    ("edinet_metrics", asof.isoformat()),
                )
                .fetchone()
            )
            self.assertEqual(row, ("ok", None))

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
                [
                    {
                        "ticker": "9682",
                        "sales_ttm": 1_000.0,
                        "extractor_revision": "a" * 64,
                    }
                ],
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
            self.assertIsNotNone(read_edinet_metrics(sqlite_path, asof))
            row = (
                sqlite3.connect(sqlite_path)
                .execute(
                    "SELECT status, error FROM source_coverage WHERE source = ? "
                    "AND coverage_key = ?",
                    ("edinet_metrics", asof.isoformat()),
                )
                .fetchone()
            )
            self.assertEqual(row, ("ok", None))

    def test_extract_edinet_metrics_reuses_cross_asof_and_same_asof(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            document = _edinet_document(ticker="96820", doc_id="S100STABLE")
            first = FakeEDINETProvider(
                documents=[document],
                zip_by_doc_id={"S100STABLE": _edinet_csv_zip()},
            )
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 24),
                    lookback_days=0,
                    provider=first,
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                ),
                0,
            )
            first_snapshot = read_edinet_metrics(sqlite_path, date(2026, 4, 24))

            cross_asof = FakeEDINETProvider(documents=[document])
            cross_output = io.StringIO()
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 25),
                    lookback_days=0,
                    provider=cross_asof,
                    sqlite_path=sqlite_path,
                    stdout=cross_output,
                ),
                0,
            )
            self.assertEqual(cross_asof.download_calls, [])
            self.assertEqual(
                read_edinet_metrics(sqlite_path, date(2026, 4, 25)),
                first_snapshot,
            )
            self.assertIn(
                "selected=1 reused=1 downloaded=0 baseline_asof=2026-04-24",
                cross_output.getvalue(),
            )

            same_asof = FakeEDINETProvider(documents=[document])
            same_output = io.StringIO()
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 25),
                    lookback_days=0,
                    provider=same_asof,
                    sqlite_path=sqlite_path,
                    stdout=same_output,
                ),
                0,
            )
            self.assertEqual(same_asof.download_calls, [])
            self.assertIn("baseline_asof=2026-04-25", same_output.getvalue())

    def test_extract_edinet_metrics_mixed_delta_matches_forced_full_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            incremental_db = root / "incremental.sqlite"
            full_db = root / "full.sqlite"
            baseline_documents = [
                _edinet_document(ticker="13010", doc_id="S100A"),
                _edinet_document(ticker="72030", doc_id="S100B"),
                _edinet_document(ticker="96820", doc_id="S100REMOVED"),
            ]
            baseline_provider = FakeEDINETProvider(
                documents=baseline_documents,
                zip_by_doc_id={
                    document["docID"]: _edinet_csv_zip() for document in baseline_documents
                },
            )
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 24),
                    lookback_days=0,
                    provider=baseline_provider,
                    sqlite_path=incremental_db,
                    stdout=io.StringIO(),
                ),
                0,
            )
            current_documents = [
                _edinet_document(ticker="13010", doc_id="S100A"),
                _edinet_document(ticker="72030", doc_id="S100BCORR", doc_type="130"),
                _edinet_document(ticker="67580", doc_id="S100NEW"),
            ]
            delta_provider = FakeEDINETProvider(
                documents=current_documents,
                zip_by_doc_id={
                    "S100BCORR": _edinet_csv_zip(),
                    "S100NEW": _edinet_csv_zip(),
                },
            )
            delta_output = io.StringIO()
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 25),
                    lookback_days=0,
                    provider=delta_provider,
                    sqlite_path=incremental_db,
                    stdout=delta_output,
                ),
                0,
            )
            self.assertEqual(
                set(delta_provider.download_calls),
                {"S100BCORR", "S100NEW"},
            )
            self.assertIn("selected=3 reused=1 downloaded=2", delta_output.getvalue())

            full_provider = FakeEDINETProvider(
                documents=current_documents,
                zip_by_doc_id={
                    document["docID"]: _edinet_csv_zip() for document in current_documents
                },
            )
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 25),
                    lookback_days=0,
                    provider=full_provider,
                    sqlite_path=full_db,
                    stdout=io.StringIO(),
                ),
                0,
            )
            incremental = read_edinet_metrics(incremental_db, date(2026, 4, 25))
            forced_full = read_edinet_metrics(full_db, date(2026, 4, 25))
            self.assertEqual(incremental, forced_full)
            assert incremental is not None
            self.assertNotIn("9682", incremental)

    def test_extract_edinet_metrics_document_edit_event_forces_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            original = _edinet_document(ticker="96820", doc_id="S100SAME")
            first = FakeEDINETProvider(
                documents=[original],
                zip_by_doc_id={"S100SAME": _edinet_csv_zip()},
            )
            extract_edinet_metrics_command(
                asof_date=date(2026, 4, 24),
                lookback_days=0,
                provider=first,
                sqlite_path=sqlite_path,
                stdout=io.StringIO(),
            )
            edit_event = {
                "docID": "S100SAME",
                "secCode": None,
                "docTypeCode": None,
                "csvFlag": None,
                "xbrlFlag": None,
                "submitDateTime": None,
                "docDescription": None,
                "periodStart": None,
                "periodEnd": None,
                "docInfoEditStatus": "1",
                "opeDateTime": "2026-04-25 09:00",
                "seqNumber": 2,
            }
            second = FakeEDINETProvider(
                documents=[original, edit_event],
                zip_by_doc_id={"S100SAME": _edinet_csv_zip()},
            )

            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 25),
                    lookback_days=0,
                    provider=second,
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                ),
                0,
            )
            self.assertEqual(second.download_calls, ["S100SAME"])

    def test_extract_edinet_metrics_empty_csv_is_failed_and_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            document = _edinet_document(ticker="96820", doc_id="S100EMPTY")
            empty_buffer = io.BytesIO()
            with zipfile.ZipFile(empty_buffer, "w") as archive:
                archive.writestr(
                    "XBRL_TO_CSV/empty.csv",
                    "要素ID\tコンテキストID\t値\n".encode("utf-16"),
                )
            first = FakeEDINETProvider(
                documents=[document],
                zip_by_doc_id={"S100EMPTY": empty_buffer.getvalue()},
            )

            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 24),
                    lookback_days=0,
                    provider=first,
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                ),
                1,
            )
            self.assertIsNone(read_edinet_metrics(sqlite_path, date(2026, 4, 24)))

            second = FakeEDINETProvider(
                documents=[document],
                zip_by_doc_id={"S100EMPTY": _edinet_csv_zip()},
            )
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 25),
                    lookback_days=0,
                    provider=second,
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                ),
                0,
            )
            self.assertEqual(second.download_calls, ["S100EMPTY"])

    def test_extract_edinet_metrics_skips_failed_baseline_and_excludes_future(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            document = _edinet_document(ticker="96820", doc_id="S100BASE")
            first = FakeEDINETProvider(
                documents=[document],
                zip_by_doc_id={"S100BASE": _edinet_csv_zip()},
            )
            extract_edinet_metrics_command(
                asof_date=date(2026, 4, 24),
                lookback_days=0,
                provider=first,
                sqlite_path=sqlite_path,
                stdout=io.StringIO(),
            )
            store_edinet_metrics(
                sqlite_path,
                date(2026, 4, 25),
                [],
                status="failed",
                error="transient failure",
            )
            after_failure = FakeEDINETProvider(documents=[document])
            after_failure_output = io.StringIO()
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 26),
                    lookback_days=0,
                    provider=after_failure,
                    sqlite_path=sqlite_path,
                    stdout=after_failure_output,
                ),
                0,
            )
            self.assertEqual(after_failure.download_calls, [])
            self.assertIn("baseline_asof=2026-04-24", after_failure_output.getvalue())

            before_all_snapshots = FakeEDINETProvider(
                documents=[document],
                zip_by_doc_id={"S100BASE": _edinet_csv_zip()},
            )
            before_output = io.StringIO()
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 23),
                    lookback_days=0,
                    provider=before_all_snapshots,
                    sqlite_path=sqlite_path,
                    stdout=before_output,
                ),
                0,
            )
            self.assertEqual(before_all_snapshots.download_calls, ["S100BASE"])
            self.assertIn("baseline_asof=none", before_output.getvalue())

    def test_extract_edinet_metrics_rejects_corrupt_latest_ok_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            document = _edinet_document(ticker="96820", doc_id="S100BASE")
            first = FakeEDINETProvider(
                documents=[document],
                zip_by_doc_id={"S100BASE": _edinet_csv_zip()},
            )
            extract_edinet_metrics_command(
                asof_date=date(2026, 4, 24),
                lookback_days=0,
                provider=first,
                sqlite_path=sqlite_path,
                stdout=io.StringIO(),
            )
            with sqlite3.connect(sqlite_path) as connection:
                connection.execute(
                    "UPDATE source_coverage SET record_count = 2 "
                    "WHERE source = 'edinet_metrics' AND coverage_key = '2026-04-24'"
                )
            provider = FakeEDINETProvider(documents=[document])
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 25),
                    lookback_days=0,
                    provider=provider,
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                )
            self.assertEqual(exit_code, 1)
            self.assertEqual(provider.download_calls, [])
            self.assertIn("baseline is corrupt", stderr.getvalue())
            self.assertIsNone(read_edinet_metrics(sqlite_path, date(2026, 4, 25)))

    def test_extract_edinet_metrics_revision_mismatch_rebuilds(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            document = _edinet_document(ticker="96820", doc_id="S100BASE")
            first = FakeEDINETProvider(
                documents=[document],
                zip_by_doc_id={"S100BASE": _edinet_csv_zip()},
            )
            extract_edinet_metrics_command(
                asof_date=date(2026, 4, 24),
                lookback_days=0,
                provider=first,
                sqlite_path=sqlite_path,
                stdout=io.StringIO(),
            )
            with sqlite3.connect(sqlite_path) as connection:
                connection.execute(
                    "UPDATE edinet_metrics SET extractor_revision = ?",
                    ("0" * 64,),
                )
            second = FakeEDINETProvider(
                documents=[document],
                zip_by_doc_id={"S100BASE": _edinet_csv_zip()},
            )
            output = io.StringIO()
            self.assertEqual(
                extract_edinet_metrics_command(
                    asof_date=date(2026, 4, 25),
                    lookback_days=0,
                    provider=second,
                    sqlite_path=sqlite_path,
                    stdout=output,
                ),
                0,
            )
            self.assertEqual(second.download_calls, ["S100BASE"])
            self.assertIn("full_rebuild_reason=incompatible_revision", output.getvalue())
            with sqlite3.connect(sqlite_path) as connection:
                revision = connection.execute(
                    "SELECT extractor_revision FROM edinet_metrics WHERE asof_date = '2026-04-25'"
                ).fetchone()[0]
            self.assertEqual(revision, compute_extractor_revision())


class IndexNextEarningsTests(unittest.TestCase):
    def test_picks_earliest_future_announcement_per_ticker(self) -> None:
        records = [
            JPXEarningsCalendarEntry(ticker="1301", announcement_date=date(2026, 5, 13)),
            JPXEarningsCalendarEntry(ticker="1301", announcement_date=date(2026, 8, 13)),
            JPXEarningsCalendarEntry(ticker="2914", announcement_date=date(2026, 5, 8)),
        ]
        result = _index_next_earnings(records, date(2026, 4, 25))
        self.assertEqual(result, {"1301": date(2026, 5, 13), "2914": date(2026, 5, 8)})

    def test_skips_announcements_before_asof(self) -> None:
        records = [
            JPXEarningsCalendarEntry(ticker="1301", announcement_date=date(2026, 4, 20)),
            JPXEarningsCalendarEntry(ticker="1301", announcement_date=date(2026, 5, 13)),
        ]
        result = _index_next_earnings(records, date(2026, 4, 25))
        self.assertEqual(result, {"1301": date(2026, 5, 13)})

    def test_keeps_alphanumeric_ticker(self) -> None:
        records = [
            JPXEarningsCalendarEntry(ticker="130A", announcement_date=date(2026, 5, 13)),
        ]
        result = _index_next_earnings(records, date(2026, 4, 25))
        self.assertEqual(result, {"130A": date(2026, 5, 13)})


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


def _edinet_document(
    *,
    ticker: str,
    doc_id: str,
    doc_type: str = "120",
) -> dict[str, object]:
    return {
        "docID": doc_id,
        "secCode": ticker,
        "docTypeCode": doc_type,
        "csvFlag": "1",
        "xbrlFlag": "1",
        "periodStart": "2025-04-01",
        "periodEnd": "2026-03-31",
        "submitDateTime": "2026-04-01 12:00",
    }


def _seed_trading_days(sqlite_path: Path, start: date, end: date) -> None:
    """Weekday rows so the weekly-margin candidates have a calendar to derive from."""
    from baibai_engine.screening.sqlite_cache import open_connection

    conn = open_connection(sqlite_path)
    try:
        day = start
        rows = []
        while day <= end:
            if day.weekday() < 5:
                rows.append(("7203", day.isoformat(), 1.0, 1.0))
            day += timedelta(days=1)
        conn.executemany(
            "INSERT OR REPLACE INTO jquants_daily_bars"
            "(ticker, traded_at, close, adjustment_close) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


class BackfillHistoryTests(unittest.TestCase):
    def test_each_range_source_is_fetched_over_the_named_window(self) -> None:
        # The window is stated once rather than derived from an as-of, so a decade of
        # history costs one pass instead of one 1200-day re-fetch per cohort date.
        provider = FakeJQuantsProvider()
        output = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _seed_trading_days(sqlite_path, date(2016, 8, 1), date(2018, 3, 1))
            code = backfill_history_command(
                start=date(2016, 8, 1),
                end=date(2018, 3, 1),
                providers=ProviderBundle(jquants=provider, edinet=None, jpx=FakeJPXProvider()),
                sqlite_path=sqlite_path,
                stdout=output,
            )

        self.assertEqual(code, 0)
        # The row readers answer with the whole window, so the two of them that can
        # span years are asked a year at a time. The interior boundaries follow the
        # calendar, not `start`, so a run naming a different first date reuses the
        # same fetch chunks. The calendar is one provider call and stays whole.
        by_source: dict[str, list[tuple[date, date]]] = {}
        for name, start, end in provider.calls:
            by_source.setdefault(name, []).append((start, end))
        year_spans = [
            (date(2016, 8, 1), date(2016, 12, 31)),
            (date(2017, 1, 1), date(2017, 12, 31)),
            (date(2018, 1, 1), date(2018, 3, 1)),
        ]
        self.assertEqual(by_source["get_eq_bars_daily_range"], year_spans)
        self.assertEqual(by_source["get_fin_summary_range"], year_spans)
        self.assertEqual(by_source["get_mkt_calendar"], [(date(2016, 8, 1), date(2018, 3, 1))])
        self.assertIn("backfill-history done", output.getvalue())

    def test_a_historical_window_keeps_its_own_final_week(self) -> None:
        # `end` here is a named past boundary, not today, so its week is complete
        # and holds a real balance date. Treating it as in-progress would drop the
        # only candidate and report a backfill that fetched nothing as success.
        provider = FakeJQuantsProvider()
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _seed_trading_days(sqlite_path, date(2026, 7, 1), date(2026, 7, 31))
            output = io.StringIO()
            code = backfill_history_command(
                start=date(2026, 7, 20),
                end=date(2026, 7, 24),
                providers=ProviderBundle(jquants=provider, edinet=None, jpx=FakeJPXProvider()),
                sqlite_path=sqlite_path,
                stdout=output,
            )

        self.assertEqual(code, 0)
        weeks = [
            start for name, start, _ in provider.calls if name == "get_mkt_margin_interest_week"
        ]
        self.assertEqual(weeks, [date(2026, 7, 24)])

    def test_a_window_with_no_complete_week_fails_rather_than_reporting_success(self) -> None:
        provider = FakeJQuantsProvider()
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _seed_trading_days(sqlite_path, date(2026, 7, 1), date(2026, 7, 31))
            with contextlib.redirect_stderr(errors):
                code = backfill_history_command(
                    start=date(2026, 7, 25),
                    end=date(2026, 7, 26),
                    providers=ProviderBundle(jquants=provider, edinet=None, jpx=FakeJPXProvider()),
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                )

        self.assertEqual(code, 1)
        self.assertIn("no weekly margin balance date candidates", errors.getvalue())

    def test_a_source_that_fails_does_not_stop_the_others(self) -> None:
        # A provider that cannot answer for one source says nothing about the rest,
        # and a run of this length should not lose hours of work to one refusal.
        class FailingBars(FakeJQuantsProvider):
            def get_eq_bars_daily_range(self, start: date, end: date) -> list[JQuantsDailyBar]:
                self.calls.append(("get_eq_bars_daily_range", start, end))
                raise JQuantsProviderError("bars unavailable")

        provider = FailingBars()
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = backfill_history_command(
                start=date(2016, 8, 1),
                end=date(2016, 8, 31),
                providers=ProviderBundle(jquants=provider, edinet=None, jpx=FakeJPXProvider()),
                sqlite_path=Path("stores/market/market.sqlite"),
                stdout=io.StringIO(),
            )

        self.assertEqual(code, 1)
        self.assertIn("get_fin_summary_range", [call[0] for call in provider.calls])
        self.assertIn("get_mkt_calendar", [call[0] for call in provider.calls])
        self.assertIn("daily_bars", errors.getvalue())
