from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.cli import ProviderBundle, bootstrap_cache_command, run_command
from baibai_loop.screening.config import ScreeningConfig
from baibai_loop.screening.providers.edinet import EdinetMetricRecord
from baibai_loop.screening.providers.jpx import JPXProviderError, JPXRegulationSnapshot
from baibai_loop.screening.providers.jquants import JQuantsDailyBar, JQuantsFinancialSummary, JQuantsMarketCalendarDay
from baibai_loop.screening.render import JST, build_output_path
from baibai_loop.screening.schema import SecurityMaster, TTMQuality


@dataclass
class FakeJQuantsProvider:
    business_day: bool = True

    def get_mkt_calendar(self, start: date, end: date) -> list[JQuantsMarketCalendarDay]:
        del end
        return [JQuantsMarketCalendarDay(day=start, is_business_day=self.business_day)]

    def get_eq_master(self) -> list[SecurityMaster]:
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
        del start
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
        del start, end
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
                ordinary_profit=None,
                profit=None,
            ),
        ]

    def get_eq_earnings_cal(self, start: date, end: date) -> list[dict[str, str]]:
        del start, end
        return []

    def bootstrap_cache(self, start: date, end: date) -> dict[str, int]:
        del start, end
        return {"ok": 1}


@dataclass
class FakeEDINETProvider:
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
            )
        }

    def bootstrap_cache(self, start: date, end: date) -> dict[str, int]:
        del start, end
        return {"ok": 1}


@dataclass
class FakeJPXProvider:
    fail_bootstrap: bool = False
    cache_exists: bool = True
    snapshots_requested: int = 0

    def get_regulation_snapshot(self, asof_date: date) -> JPXRegulationSnapshot:
        del asof_date
        self.snapshots_requested += 1
        return JPXRegulationSnapshot(flags_by_ticker={}, source_names=("jpx-public-csv",))

    def has_regulation_cache(self, asof_date: date) -> bool:
        del asof_date
        return self.cache_exists

    def bootstrap_cache(self, asof_date: date) -> dict[str, int]:
        del asof_date
        if self.fail_bootstrap:
            raise JPXProviderError("missing jpx source")
        return {"ok": 1}


class ScreeningCliTests(unittest.TestCase):
    def test_run_command_writes_screened_markdown(self) -> None:
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
                exit_code = run_command(
                    date(2026, 4, 24),
                    config,
                    providers,
                    now=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
                )
                self.assertEqual(exit_code, 2)
                output_path = build_output_path(date(2026, 4, 24))
                self.assertTrue(output_path.exists())
                rendered = output_path.read_text(encoding="utf-8")
                self.assertIn('run_date: "2026-04-24"', rendered)
                self.assertIn("ttm_quality 集計: exact=1, approximated=1, unavailable=1", rendered)
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

    def test_bootstrap_cache_command_tolerates_jpx_bootstrap_failure(self) -> None:
        exit_code = bootstrap_cache_command(
            date(2026, 4, 1),
            date(2026, 4, 24),
            ProviderBundle(
                jquants=FakeJQuantsProvider(),
                edinet=FakeEDINETProvider(),
                jpx=FakeJPXProvider(fail_bootstrap=True),
            ),
        )
        self.assertEqual(exit_code, 0)
