from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.cli import (
    ProviderBundle,
    _index_next_earnings,
    bootstrap_cache_command,
    extract_edinet_metrics_command,
    run_command,
    select_command,
)
from baibai_loop.screening.config import ScreeningConfig
from baibai_loop.screening.providers.edinet import EdinetMetricRecord
from baibai_loop.screening.providers.jpx import JPXProviderError, JPXRegulationSnapshot
from baibai_loop.screening.providers.jquants import (
    JQuantsDailyBar,
    JQuantsFinancialSummary,
    JQuantsMarketCalendarDay,
)
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
    documents: list[dict[str, object]] | None = None
    zip_by_doc_id: dict[str, bytes] | None = None

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

    def list_documents(self, on_date: date) -> list[dict[str, object]]:
        del on_date
        return list(self.documents or [])

    def download_csv_zip(self, doc_id: str) -> bytes:
        return (self.zip_by_doc_id or {})[doc_id]

    def bootstrap_cache(self, start: date, end: date) -> dict[str, int]:
        del start, end
        return {"ok": 1}


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

    def get_regulation_snapshot(self, asof_date: date) -> JPXRegulationSnapshot:
        del asof_date
        self.snapshots_requested += 1
        return JPXRegulationSnapshot(flags_by_ticker={}, source_names=self.source_names)

    def has_regulation_cache(self, asof_date: date) -> bool:
        del asof_date
        return self.cache_exists

    def bootstrap_cache(self, asof_date: date) -> dict[str, int]:
        del asof_date
        if self.fail_bootstrap:
            raise JPXProviderError("missing jpx source")
        return {"ok": 1}


class ScreeningCliTests(unittest.TestCase):
    def test_run_command_writes_candidates_yaml(self) -> None:
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
                self.assertIn("ttm_quality_counts:", rendered)
                payload = yaml.safe_load(rendered)
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
                self.assertRegex(payload["run_id"], r"^screening-20260424-[0-9a-f]{8}$")
                self.assertRegex(payload["config_hash"], r"^[0-9a-f]{16}$")
                self.assertRegex(payload["cache_manifest_hash"], r"^[0-9a-f]{16}$")
                manifest_path = Path(".cache/screening/manifests") / f"{payload['run_id']}.json"
                self.assertTrue(manifest_path.exists())
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertEqual(manifest["run_id"], payload["run_id"])
                self.assertEqual(manifest["config_hash"], payload["config_hash"])
                self.assertEqual(
                    manifest["cache_manifest_hash"],
                    payload["cache_manifest_hash"],
                )
            finally:
                os.chdir(cwd)

    def test_run_command_omits_edinet_source_when_optional_provider_is_absent(self) -> None:
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
                exit_code = run_command(
                    date(2026, 4, 24),
                    config,
                    providers,
                    now=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
                )
                self.assertEqual(exit_code, 2)
                payload = yaml.safe_load(build_output_path(date(2026, 4, 24)).read_text())
                self.assertEqual(
                    payload["data_sources"], ["j-quants-light", "jpx-public-regulation"]
                )
                self.assertIn(
                    "EDINET preprocessed metrics: optional unavailable",
                    payload["provider_status_lines"],
                )
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

    def test_extract_edinet_metrics_command_writes_parsed_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "raw"
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
                cache_dir=cache_dir,
                stdout=buffer,
            )
            self.assertEqual(exit_code, 0)
            output_path = cache_dir / "edinet" / "metrics" / "2026-04-24.json"
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload[0]["ticker"], "9682")
            self.assertEqual(payload[0]["net_cash"], 600.0)
            self.assertEqual(payload[0]["fcf_ttm"], 700.0)
            self.assertEqual(payload[0]["source_submit_datetime"], "2026-04-01 12:00")
            self.assertEqual(payload[0]["source_period_start"], "2025-04-01")
            self.assertEqual(payload[0]["source_period_end"], "2026-03-31")
            self.assertIn("1 records", buffer.getvalue())

    def test_extract_edinet_metrics_command_returns_zero_for_quality_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "raw"
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
                cache_dir=cache_dir,
                stdout=buffer,
            )

            self.assertEqual(exit_code, 0)
            payload = json.loads(
                (cache_dir / "edinet" / "metrics" / "2026-04-24.json").read_text(encoding="utf-8")
            )
            self.assertIn("debt_assumed_zero", payload[0]["failure_reasons"])
            self.assertIn("1 records with quality issues", buffer.getvalue())


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
        payload = yaml.safe_dump({"candidates": candidates}, allow_unicode=True, sort_keys=False)
        path.write_text(payload, encoding="utf-8")
        return path

    def _write_outlook(self, root: Path, asof: date, sectors: dict[str, str | None]) -> Path:
        path = root / f"{asof:%Y}" / f"{asof:%m}" / f"outlook-{asof:%Y-%m-%d}-bootstrap.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        sectors_payload = {
            sector: {"status": status, "rationale": "test", "source_refs": []}
            for sector, status in sectors.items()
        }
        path.write_text(
            yaml.safe_dump({"sectors": sectors_payload}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return path

    def test_filters_headwind_sectors_and_ranks_by_lane_aware_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            self._write_candidates(
                root / "records/03-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "headwind exclude",
                        "sector_33": "石油・石炭製品",
                        "market_cap_oku": 2000,
                        "signals": [{"name": "valuation-reversion"}],
                    },
                    {
                        "ticker": "2222",
                        "name": "single hit",
                        "sector_33": "電気機器",
                        "market_cap_oku": 600,
                        "signals": [{"name": "valuation-reversion"}],
                    },
                    {
                        "ticker": "3333",
                        "name": "triple hit",
                        "sector_33": "機械",
                        "market_cap_oku": 400,
                        "signals": [
                            {"name": "valuation-reversion"},
                            {"name": "cash-rich-asset-discount"},
                            {"name": "cashflow-yield-discount"},
                        ],
                    },
                ],
            )
            self._write_outlook(
                root / "records/02-outlook",
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
                outlook_path=None,
                top=10,
                candidates_root=root / "records/03-candidates",
                outlook_root=root / "records/02-outlook",
                stdout=buffer,
            )
            self.assertEqual(exit_code, 0)
            payload = yaml.safe_load(buffer.getvalue())
            self.assertEqual(payload["input_count"], 3)
            self.assertEqual(payload["after_outlook_filter"], 2)
            self.assertEqual(
                [c["ticker"] for c in payload["lane_toplists"]["cash-rich-asset-discount"]],
                ["3333"],
            )
            tickers = [c["ticker"] for c in payload["candidates"]]
            self.assertEqual(tickers, ["3333", "2222"])
            self.assertEqual(
                payload["research_selection_lane_order"],
                [
                    "strict-net-cash-discount",
                    "fcf-yield-discount",
                    "cash-rich-asset-discount",
                    "cashflow-yield-discount",
                    "sales-discount-growth",
                    "valuation-reversion",
                ],
            )
            self.assertEqual(payload["candidates"][0]["selection_lane"], "cash-rich-asset-discount")
            self.assertEqual(
                payload["candidates"][0]["recommendation_lane"], "cash-rich-asset-discount"
            )
            self.assertEqual(
                payload["ranked_candidates"][0]["selection_lane"], "valuation-reversion"
            )
            self.assertEqual(payload["candidates"][0]["position_tier"], "200-500")
            self.assertEqual(payload["candidates"][1]["position_tier"], "500-1000")

    def test_select_recommends_lane_diversified_candidates_before_global_rank(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            self._write_candidates(
                root / "records/03-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "valuation first in global rank",
                        "sector_33": "機械",
                        "market_cap_oku": 600,
                        "signals": [{"name": "valuation-reversion"}],
                    },
                    {
                        "ticker": "2222",
                        "name": "strict net cash first in research recommendation",
                        "sector_33": "機械",
                        "market_cap_oku": 150,
                        "signals": [
                            {
                                "name": "strict-net-cash-discount",
                                "metrics": {
                                    "net_cash_to_market_cap": 0.6,
                                    "price_to_equity": 0.7,
                                },
                            }
                        ],
                    },
                ],
            )
            self._write_outlook(
                root / "records/02-outlook",
                asof,
                sectors={"機械": "neutral"},
            )

            buffer = io.StringIO()
            exit_code = select_command(
                asof_date=asof,
                outlook_path=None,
                top=10,
                candidates_root=root / "records/03-candidates",
                outlook_root=root / "records/02-outlook",
                stdout=buffer,
            )

            self.assertEqual(exit_code, 0)
            payload = yaml.safe_load(buffer.getvalue())
            self.assertEqual(
                [c["ticker"] for c in payload["ranked_candidates"]],
                ["1111", "2222"],
            )
            self.assertEqual([c["ticker"] for c in payload["candidates"]], ["2222", "1111"])
            self.assertEqual(payload["candidates"][0]["selection_lane"], "strict-net-cash-discount")
            self.assertEqual(
                payload["candidates"][0]["recommendation_lane"], "strict-net-cash-discount"
            )

    def test_select_separates_recommendation_lane_from_primary_selection_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            self._write_candidates(
                root / "records/03-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "strict top",
                        "sector_33": "機械",
                        "market_cap_oku": 300,
                        "signals": [
                            {
                                "name": "strict-net-cash-discount",
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
                        "sector_33": "機械",
                        "market_cap_oku": 300,
                        "signals": [
                            {
                                "name": "strict-net-cash-discount",
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
            self._write_outlook(
                root / "records/02-outlook",
                asof,
                sectors={"機械": "neutral"},
            )

            buffer = io.StringIO()
            exit_code = select_command(
                asof_date=asof,
                outlook_path=None,
                top=10,
                candidates_root=root / "records/03-candidates",
                outlook_root=root / "records/02-outlook",
                stdout=buffer,
            )

            self.assertEqual(exit_code, 0)
            payload = yaml.safe_load(buffer.getvalue())
            self.assertEqual([c["ticker"] for c in payload["candidates"]], ["1111", "2222"])
            self.assertEqual(
                payload["candidates"][1]["recommendation_lane"], "sales-discount-growth"
            )
            self.assertEqual(payload["candidates"][1]["selection_lane"], "strict-net-cash-discount")

    def test_select_uses_lane_strength_before_market_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            self._write_candidates(
                root / "records/03-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "large weak cash rich",
                        "sector_33": "機械",
                        "market_cap_oku": 5000,
                        "signals": [
                            {
                                "name": "cash-rich-asset-discount",
                                "metrics": {
                                    "cash_to_market_cap": 0.41,
                                    "price_to_equity": 0.95,
                                },
                            }
                        ],
                    },
                    {
                        "ticker": "2222",
                        "name": "small strong cash rich",
                        "sector_33": "機械",
                        "market_cap_oku": 150,
                        "signals": [
                            {
                                "name": "cash-rich-asset-discount",
                                "metrics": {
                                    "cash_to_market_cap": 0.8,
                                    "price_to_equity": 0.5,
                                },
                            }
                        ],
                    },
                ],
            )
            self._write_outlook(
                root / "records/02-outlook",
                asof,
                sectors={"機械": "neutral"},
            )
            buffer = io.StringIO()
            exit_code = select_command(
                asof_date=asof,
                outlook_path=None,
                top=10,
                candidates_root=root / "records/03-candidates",
                outlook_root=root / "records/02-outlook",
                stdout=buffer,
            )
            self.assertEqual(exit_code, 0)
            payload = yaml.safe_load(buffer.getvalue())
            self.assertEqual([c["ticker"] for c in payload["candidates"]], ["2222", "1111"])
            self.assertEqual(
                [c["ticker"] for c in payload["lane_toplists"]["cash-rich-asset-discount"]],
                ["2222", "1111"],
            )
            self.assertEqual(payload["candidates"][0]["selection_lane"], "cash-rich-asset-discount")

    def test_select_uses_configured_lane_toplist_limit_independent_from_top(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            self._write_candidates(
                root / "records/03-candidates",
                asof,
                candidates=[
                    {
                        "ticker": "1111",
                        "name": "first",
                        "sector_33": "機械",
                        "market_cap_oku": 150,
                        "signals": [{"name": "cash-rich-asset-discount"}],
                    },
                    {
                        "ticker": "2222",
                        "name": "second",
                        "sector_33": "機械",
                        "market_cap_oku": 160,
                        "signals": [{"name": "cash-rich-asset-discount"}],
                    },
                ],
            )
            self._write_outlook(
                root / "records/02-outlook",
                asof,
                sectors={"機械": "neutral"},
            )

            buffer = io.StringIO()
            exit_code = select_command(
                asof_date=asof,
                outlook_path=None,
                top=1,
                candidates_root=root / "records/03-candidates",
                outlook_root=root / "records/02-outlook",
                stdout=buffer,
            )

            self.assertEqual(exit_code, 0)
            payload = yaml.safe_load(buffer.getvalue())
            self.assertEqual(payload["lane_toplist_limit"], 5)
            self.assertEqual(len(payload["candidates"]), 1)
            self.assertEqual(
                [c["ticker"] for c in payload["lane_toplists"]["cash-rich-asset-discount"]],
                ["1111", "2222"],
            )

    def test_rejects_non_mapping_candidates_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asof = date(2026, 4, 24)
            candidates_path = (
                root
                / "records/03-candidates"
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
            self._write_outlook(root / "records/02-outlook", asof, sectors={})

            buffer = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = select_command(
                    asof_date=asof,
                    outlook_path=None,
                    top=10,
                    candidates_root=root / "records/03-candidates",
                    outlook_root=root / "records/02-outlook",
                    stdout=buffer,
                )
            self.assertEqual(exit_code, 1)
            self.assertIn("invalid candidates YAML", stderr.getvalue())
            self.assertIn("must be a mapping", stderr.getvalue())
