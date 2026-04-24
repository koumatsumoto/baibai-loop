from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.providers.edinet import EDINETProviderError, normalize_metric_record, parse_sec_code
from baibai_loop.screening.providers.jpx import JPXProvider
from baibai_loop.screening.providers.jquants import (
    JQuantsProvider,
    normalize_daily_bar,
    normalize_financial_summary,
    normalize_market_calendar,
    normalize_security_master,
    parse_jquants_code,
)
from baibai_loop.screening.schema import TTMQuality


class ScreeningProviderTests(unittest.TestCase):
    def test_parse_sec_code_supports_alpha_numeric_code(self) -> None:
        self.assertEqual(parse_sec_code("130A0"), "130A")

    def test_parse_sec_code_rejects_non_zero_suffix(self) -> None:
        with self.assertRaises(EDINETProviderError):
            parse_sec_code("130A1")

    def test_parse_jquants_code_supports_zero_suffix(self) -> None:
        self.assertEqual(parse_jquants_code("130A0"), "130A")

    def test_parse_jquants_code_rejects_non_zero_suffix(self) -> None:
        self.assertEqual(parse_jquants_code("130A1"), "130A")

    def test_normalize_metric_record_parses_ttm_quality(self) -> None:
        record = normalize_metric_record(
            {
                "ticker": "7203",
                "sales_ttm": "100",
                "ocf_ttm": "10",
                "debt": "20",
                "cash": "5",
                "ebitda_ttm": "30",
                "ttm_quality_ev_ebitda": "exact",
                "ttm_quality_p_s": "approximated",
                "ttm_quality_pcfr": "unavailable",
            }
        )
        self.assertEqual(record.ttm_quality_ev_ebitda, TTMQuality.EXACT)
        self.assertEqual(record.ttm_quality_p_s, TTMQuality.APPROXIMATED)
        self.assertEqual(record.ttm_quality_pcfr, TTMQuality.UNAVAILABLE)

    def test_normalize_security_master_handles_alpha_numeric_ticker(self) -> None:
        security = normalize_security_master(
            {
                "Code": "130A",
                "CompanyName": "Alpha",
                "MarketCodeName": "Prime",
                "Sector33CodeName": "情報・通信業",
                "SecurityType": "common",
            }
        )
        self.assertEqual(security.code, "130A")
        self.assertEqual(security.market_segment, "Prime")

    def test_normalize_security_master_supports_client_v2_field_names(self) -> None:
        security = normalize_security_master(
            {
                "Code": "130A0",
                "CoName": "Alpha",
                "MktNm": "グロース",
                "S33Nm": "情報・通信業",
                "Date": "2026-04-24T00:00:00",
            }
        )
        self.assertEqual(security.code, "130A")
        self.assertEqual(security.name, "Alpha")
        self.assertEqual(security.market_segment, "グロース")
        self.assertEqual(security.sector_33, "情報・通信業")

    def test_normalize_security_master_marks_non_zero_suffix_as_non_common(self) -> None:
        security = normalize_security_master(
            {
                "Code": "25935",
                "CoName": "優先株",
                "MktNm": "プライム",
                "S33Nm": "食料品",
                "Date": "2026-04-24T00:00:00",
            }
        )
        self.assertEqual(security.code, "2593")
        self.assertFalse(security.is_common_stock)

    def test_normalize_market_calendar_business_day(self) -> None:
        calendar_day = normalize_market_calendar({"Date": "2026-04-24", "HolidayDivision": "1"})
        self.assertTrue(calendar_day.is_business_day)

    def test_normalize_market_calendar_supports_client_v2_short_key(self) -> None:
        calendar_day = normalize_market_calendar({"Date": "2026-04-24T00:00:00", "HolDiv": "1"})
        self.assertTrue(calendar_day.is_business_day)

    def test_normalize_market_calendar_treats_half_day_as_business(self) -> None:
        # HolidayDivision="2" は半日営業 (大納会など取引あり) で、business_day 扱いすべき。
        calendar_day = normalize_market_calendar({"Date": "2026-12-30", "HolDiv": "2"})
        self.assertTrue(calendar_day.is_business_day)

    def test_normalize_daily_bar_preserves_zero_turnover(self) -> None:
        # `or` チェーンで 0.0 を欠損誤判定しないことを固定化する。
        bar = normalize_daily_bar(
            {
                "Code": "13010",
                "Date": "2026-04-24T00:00:00",
                "C": 0.5,
                "Va": 0.0,
            }
        )
        self.assertIsNotNone(bar)
        assert bar is not None
        self.assertEqual(bar.turnover_value, 0.0)

    def test_normalize_financial_summary_preserves_zero_fields(self) -> None:
        # EPS=0 / OP=0 / Sales=0 が None に潰れないこと (損益分岐点ちょうど、赤字転換など)。
        summary = normalize_financial_summary(
            {
                "Code": "88910",
                "DiscDate": "2026-04-01T00:00:00",
                "FEPS": "10.0",
                "EPS": "0.0",
                "BPS": "1200.0",
                "Sales": "0",
                "OP": "0",
                "ShOutFY": "1000000",
            }
        )
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.eps_ttm, 0.0)
        self.assertEqual(summary.sales, 0.0)
        self.assertEqual(summary.operating_profit, 0.0)

    def test_normalize_daily_bar_supports_client_v2_short_keys(self) -> None:
        bar = normalize_daily_bar(
            {
                "Code": "13010",
                "Date": "2026-04-24T00:00:00",
                "C": 5070.0,
                "Va": 526318500.0,
            }
        )
        self.assertIsNotNone(bar)
        assert bar is not None
        self.assertEqual(bar.ticker, "1301")
        self.assertEqual(bar.close, 5070.0)
        self.assertEqual(bar.turnover_value, 526318500.0)

    def test_normalize_daily_bar_skips_non_common_suffix(self) -> None:
        bar = normalize_daily_bar(
            {
                "Code": "25935",
                "Date": "2026-04-24T00:00:00",
                "C": 100.0,
            }
        )
        self.assertIsNone(bar)

    def test_normalize_financial_summary_supports_client_v2_short_keys(self) -> None:
        summary = normalize_financial_summary(
            {
                "Code": "88910",
                "DiscDate": "2026-04-01T00:00:00",
                "FEPS": "377.75",
                "EPS": "300.50",
                "BPS": "1200.0",
                "ShOutFY": "1000000",
                "Sales": "31300000000",
                "OP": "1840000000",
                "OdP": "1720000000",
                "NP": "1070000000",
            }
        )
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.ticker, "8891")
        self.assertEqual(summary.forecast_eps, 377.75)
        self.assertEqual(summary.eps_ttm, 300.50)
        self.assertEqual(summary.shares_outstanding, 1000000.0)
        self.assertEqual(summary.operating_profit, 1840000000.0)

    def test_normalize_financial_summary_skips_non_common_suffix(self) -> None:
        summary = normalize_financial_summary(
            {
                "Code": "25935",
                "DiscDate": "2026-04-24T00:00:00",
                "EPS": "10.0",
            }
        )
        self.assertIsNone(summary)

    def test_get_eq_bars_daily_range_chunks_long_windows(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def get_eq_bars_daily_range(self, start_dt: str, end_dt: str) -> list[dict[str, object]]:
                self.calls.append((start_dt, end_dt))
                return [
                    {
                        "Code": "13010",
                        "Date": f"{start_dt}T00:00:00",
                        "C": 100.0,
                        "Va": 1000.0,
                    }
                ]

        client = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            provider = JQuantsProvider("token", Path(tmp), client=client)
            with patch("baibai_loop.screening.providers.jquants.time.sleep", return_value=None):
                bars = provider.get_eq_bars_daily_range(date(2026, 1, 1), date(2026, 2, 15))

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0], ("2026-01-01", "2026-01-31"))
        self.assertEqual(client.calls[1], ("2026-02-01", "2026-02-15"))
        self.assertEqual(len(bars), 2)

    def test_get_eq_bars_daily_range_skips_sleep_on_cache_hit(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def get_eq_bars_daily_range(self, start_dt: str, end_dt: str) -> list[dict[str, object]]:
                self.calls.append((start_dt, end_dt))
                return [
                    {
                        "Code": "13010",
                        "Date": f"{start_dt}T00:00:00",
                        "C": 100.0,
                        "Va": 1000.0,
                    }
                ]

        client = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            provider = JQuantsProvider("token", Path(tmp), client=client)
            with patch("baibai_loop.screening.providers.jquants.time.sleep") as sleep_first:
                first = provider.get_eq_bars_daily_range(date(2026, 1, 1), date(2026, 2, 15))
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(sleep_first.call_count, 1)

            with patch("baibai_loop.screening.providers.jquants.time.sleep") as sleep_second:
                second = provider.get_eq_bars_daily_range(date(2026, 1, 1), date(2026, 2, 15))
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(sleep_second.call_count, 0)
            self.assertEqual(len(second), len(first))

    def test_get_eq_bars_daily_range_retries_429(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls = 0

            def get_eq_bars_daily_range(self, start_dt: str, end_dt: str) -> list[dict[str, object]]:
                del start_dt, end_dt
                self.calls += 1
                if self.calls < 3:
                    raise RuntimeError("too many 429 error responses")
                return [{"Code": "13010", "Date": "2026-04-24T00:00:00", "C": 100.0, "Va": 1000.0}]

        client = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            provider = JQuantsProvider("token", Path(tmp), client=client)
            with patch("baibai_loop.screening.providers.jquants.time.sleep", return_value=None):
                bars = provider.get_eq_bars_daily_range(date(2026, 4, 24), date(2026, 4, 24))

        self.assertEqual(client.calls, 3)
        self.assertEqual(len(bars), 1)

    def test_jpx_parse_csv_rows_supports_cp932(self) -> None:
        provider = JPXProvider(Path("/tmp"))
        rows = provider._parse_csv_rows("コード,規制区分\n3856,整理銘柄\n".encode("cp932"), "https://example.com/sample.csv")
        self.assertEqual(rows, [{"コード": "3856", "規制区分": "整理銘柄"}])

    def test_jpx_parse_special_alert_margin_rows_extracts_marked_codes(self) -> None:
        class FakeFrame:
            def __init__(self, rows: list[list[str]]) -> None:
                self._rows = rows

            def fillna(self, value: str) -> "FakeFrame":
                del value
                return self

            @property
            def iloc(self) -> "FakeFrame":
                return self

            def __getitem__(self, item: slice) -> "FakeFrame":
                return FakeFrame(self._rows[item])

            def iterrows(self):
                for index, row in enumerate(self._rows):
                    yield index, SimpleNamespace(tolist=lambda row=row: row)

        class FakePandas:
            @staticmethod
            def read_excel(*args, **kwargs) -> FakeFrame:
                del args, kwargs
                return FakeFrame(
                    [[""] * 7 for _ in range(7)]
                    + [
                        ["B", "○", "", "Ａｂａｌａｎｃｅ　普通株式", "スタンダード", "制", "38560"],
                        ["B", "規", "", "地域新聞社　普通株式", "スタンダード", "制", "21640"],
                        ["B", "○", "", "旅工房　普通株式", "グロース", "制", "65480"],
                    ]
                )

        provider = JPXProvider(Path("/tmp"))
        rows = provider._parse_special_alert_margin_rows(FakePandas(), b"", "特別注意銘柄", "https://example.com/mtdaily.xls")
        self.assertEqual(
            rows,
            [
                {"code": "38560", "flag": "特別注意銘柄"},
                {"code": "65480", "flag": "特別注意銘柄"},
            ],
        )
