from __future__ import annotations

import json
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
from baibai_loop.screening.providers.jpx import JPXProvider, JPXProviderError
from baibai_loop.screening.providers.jquants import (
    JQuantsProvider,
    normalize_daily_bar,
    normalize_financial_summary,
    normalize_market_calendar,
    normalize_security_master,
    parse_jquants_code,
)
from baibai_loop.screening.schema import TTMQuality


class _FixedHtmlSession:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def get(self, url: str, timeout: int):
        del url, timeout

        class _Response:
            def __init__(self, content: bytes) -> None:
                self.content = content
                self.status_code = 200
                self.headers = {"content-type": "text/html; charset=UTF-8"}

        return _Response(self._content)


class ScreeningProviderTests(unittest.TestCase):
    def _read_jpx_fixture(self, name: str) -> bytes:
        return (ROOT / "tests" / "fixtures" / "jpx" / name).read_bytes()

    def test_parse_sec_code_supports_alpha_numeric_code(self) -> None:
        self.assertEqual(parse_sec_code("130A0"), "130A")

    def test_parse_sec_code_rejects_non_zero_suffix(self) -> None:
        with self.assertRaises(EDINETProviderError):
            parse_sec_code("130A1")

    def test_parse_jquants_code_supports_zero_suffix(self) -> None:
        self.assertEqual(parse_jquants_code("130A0"), "130A")

    def test_parse_jquants_code_truncates_non_zero_suffix(self) -> None:
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

    def test_normalize_security_master_normalizes_half_width_middle_dot_in_sector(self) -> None:
        # J-Quants payloads use both U+FF65 ("情報･通信業") and U+30FB
        # ("情報・通信業") for the same TSE 33 sector. Normalize to full-width.
        security = normalize_security_master(
            {
                "Code": "130A",
                "CoName": "Alpha",
                "MktNm": "プライム",
                "S33Nm": "情報･通信業",
                "Date": "2026-04-24T00:00:00",
            }
        )
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

    def test_normalize_daily_bar_preserves_adjustment_close(self) -> None:
        bar = normalize_daily_bar(
            {
                "Code": "13010",
                "Date": "2026-04-24T00:00:00",
                "Close": 200.0,
                "AdjustmentClose": 100.0,
                "Va": 526318500.0,
            }
        )
        self.assertIsNotNone(bar)
        assert bar is not None
        self.assertEqual(bar.close, 200.0)
        self.assertEqual(bar.adjustment_close, 100.0)

    def test_normalize_daily_bar_leaves_adjustment_close_none_when_missing(self) -> None:
        bar = normalize_daily_bar(
            {
                "Code": "13010",
                "Date": "2026-04-24T00:00:00",
                "C": 100.0,
                "Va": 526318500.0,
            }
        )
        self.assertIsNotNone(bar)
        assert bar is not None
        self.assertEqual(bar.close, 100.0)
        self.assertIsNone(bar.adjustment_close)

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

    def test_normalize_financial_summary_preserves_fiscal_period_fields(self) -> None:
        summary = normalize_financial_summary(
            {
                "Code": "88910",
                "DisclosedDate": "2026-04-01",
                "TypeOfCurrentPeriod": "1Q",
                "CurrentFiscalYearEndDate": "2027-03-31",
                "CurrentPeriodStartDate": "2026-04-01",
                "CurrentPeriodEndDate": "2026-06-30",
                "EarningsPerShare": "300.50",
            }
        )
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.fiscal_period, "1Q")
        self.assertEqual(summary.fiscal_year_end, date(2027, 3, 31))
        self.assertEqual(summary.period_start, date(2026, 4, 1))
        self.assertEqual(summary.period_end, date(2026, 6, 30))

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

    def test_jpx_resolves_latest_special_attention_xls_from_index(self) -> None:
        index_url = "https://www.jpx.co.jp/markets/statistics-equities/margin/index.html"
        provider = JPXProvider(
            Path("/tmp"),
            session=_FixedHtmlSession(self._read_jpx_fixture("special_caution_index.html")),
            special_caution_index_url=index_url,
        )

        resolved = provider._resolve_special_attention_xls_url(date(2026, 4, 24))

        self.assertEqual(
            resolved,
            "https://www.jpx.co.jp/markets/statistics-equities/margin/"
            "tvdivq0000001r92-att/mtdailyk2026042300.xls",
        )

    def test_jpx_resolve_special_attention_xls_rejects_non_jpx_link(self) -> None:
        html = """
        <html><body>
          <a href="https://example.com/mtdailyk2026042300.xls">Excel</a>
          <p>特別注意銘柄について信用取引残高を日々公表しています。</p>
        </body></html>
        """.encode("utf-8")
        provider = JPXProvider(
            Path("/tmp"),
            session=_FixedHtmlSession(html),
            special_caution_index_url="https://www.jpx.co.jp/markets/statistics-equities/margin/index.html",
        )

        with self.assertRaisesRegex(JPXProviderError, "https://www\\.jpx\\.co\\.jp"):
            provider._resolve_special_attention_xls_url(date(2026, 4, 24))

    def test_jpx_resolve_special_attention_xls_fails_when_no_candidates(self) -> None:
        html = """
        <html><body>
          <a href="/markets/statistics-equities/margin/readme.pdf">PDF</a>
          <p>特別注意銘柄について信用取引残高を日々公表しています。</p>
        </body></html>
        """.encode("utf-8")
        provider = JPXProvider(
            Path("/tmp"),
            session=_FixedHtmlSession(html),
            special_caution_index_url="https://www.jpx.co.jp/markets/statistics-equities/margin/index.html",
        )

        with self.assertRaisesRegex(JPXProviderError, "failed to locate JPX special caution Excel link"):
            provider._resolve_special_attention_xls_url(date(2026, 4, 24))

    def test_jpx_resolve_special_attention_xls_raises_on_index_http_error(self) -> None:
        class FakeResponse:
            content = b""
            status_code = 503
            headers: dict[str, str] = {}

        class FakeSession:
            def get(self, url: str, timeout: int) -> FakeResponse:
                del url, timeout
                return FakeResponse()

        provider = JPXProvider(
            Path("/tmp"),
            session=FakeSession(),
            special_caution_index_url="https://www.jpx.co.jp/markets/statistics-equities/margin/index.html",
        )

        with self.assertRaisesRegex(JPXProviderError, "failed to download JPX special caution index"):
            provider._resolve_special_attention_xls_url(date(2026, 4, 24))

    def test_jpx_resolve_special_attention_xls_falls_back_to_last_link_without_date_token(self) -> None:
        html = """
        <html><body>
          <h2>個別銘柄信用取引残高表</h2>
          <a href="/markets/statistics-equities/margin/tvdivq0000001r92-att/mtdailyk.xls">old</a>
          <a href="/markets/statistics-equities/margin/tvdivq0000001r92-att/mtdailyk-latest.xls">latest</a>
        </body></html>
        """.encode("utf-8")
        provider = JPXProvider(
            Path("/tmp"),
            session=_FixedHtmlSession(html),
            special_caution_index_url="https://www.jpx.co.jp/markets/statistics-equities/margin/index.html",
        )

        self.assertEqual(
            provider._resolve_special_attention_xls_url(date(2026, 4, 24)),
            "https://www.jpx.co.jp/markets/statistics-equities/margin/"
            "tvdivq0000001r92-att/mtdailyk-latest.xls",
        )

    def test_jpx_resolve_special_attention_xls_prefers_later_link_for_same_date(self) -> None:
        html = """
        <html><body>
          <a href="/markets/statistics-equities/margin/tvdivq0000001r92-att/mtdailyk2026042300.xls">old</a>
          <a href="/markets/statistics-equities/margin/tvdivq0000001r92-att/mtdailyk2026042301.xls">revised</a>
        </body></html>
        """.encode("utf-8")
        provider = JPXProvider(
            Path("/tmp"),
            session=_FixedHtmlSession(html),
            special_caution_index_url="https://www.jpx.co.jp/markets/statistics-equities/margin/index.html",
        )

        self.assertEqual(
            provider._resolve_special_attention_xls_url(date(2026, 4, 24)),
            "https://www.jpx.co.jp/markets/statistics-equities/margin/"
            "tvdivq0000001r92-att/mtdailyk2026042301.xls",
        )

    def test_jpx_get_regulation_snapshot_resolves_special_attention_index_before_download(self) -> None:
        class CapturingProvider(JPXProvider):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.downloaded_urls: list[str] = []

            def _download_rows(self, source_name: str, url: str) -> list[dict[str, str]]:
                self.downloaded_urls.append(url)
                return [{"code": "38560", "flag": source_name}]

        index_url = "https://www.jpx.co.jp/markets/statistics-equities/margin/index.html"
        with tempfile.TemporaryDirectory() as tmp:
            provider = CapturingProvider(
                Path(tmp),
                session=_FixedHtmlSession(self._read_jpx_fixture("special_caution_index.html")),
                special_caution_index_url=index_url,
            )
            snapshot = provider.get_regulation_snapshot(date(2026, 4, 24))

        self.assertEqual(snapshot.flags_by_ticker, {"3856": ("特別注意銘柄",)})
        self.assertEqual(
            provider.downloaded_urls,
            [
                "https://www.jpx.co.jp/markets/statistics-equities/margin/"
                "tvdivq0000001r92-att/mtdailyk2026042300.xls",
            ],
        )

    def test_jpx_download_rows_rejects_non_jpx_origin(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def get(self, url: str, timeout: int):
                del timeout
                self.calls.append(url)
                raise AssertionError("request should not be attempted")

        session = FakeSession()
        provider = JPXProvider(Path("/tmp"), session=session)

        with self.assertRaisesRegex(JPXProviderError, "https://www\\.jpx\\.co\\.jp"):
            provider._download_rows("整理銘柄", "https://example.com/listing/market-alerts/supervision/")

        self.assertEqual(session.calls, [])

    def test_jpx_parse_reorganization_html_rows_from_content_type(self) -> None:
        class FakeResponse:
            def __init__(self, content: bytes) -> None:
                self.content = content
                self.status_code = 200
                self.headers = {"content-type": "text/html; charset=UTF-8"}

        class FakeSession:
            def __init__(self, response: FakeResponse) -> None:
                self._response = response

            def get(self, url: str, timeout: int) -> FakeResponse:
                del url, timeout
                return self._response

        provider = JPXProvider(
            Path("/tmp"),
            session=FakeSession(FakeResponse(self._read_jpx_fixture("reorganization.html"))),
        )
        rows = provider._download_rows("整理銘柄", "https://www.jpx.co.jp/listing/market-alerts/supervision/")

        self.assertEqual(
            rows,
            [
                {"code": "4917", "flag": "整理銘柄"},
                {"code": "7426", "flag": "整理銘柄"},
            ],
        )

    def test_jpx_parse_trading_halt_html_rows_extracts_codes(self) -> None:
        provider = JPXProvider(Path("/tmp"))

        rows = provider._parse_html_rows(
            "取引停止",
            self._read_jpx_fixture("trading_halt.html"),
            "https://www.jpx.co.jp/markets/equities/suspended/",
        )

        self.assertEqual(rows, [{"code": "9941", "flag": "取引停止"}])

    def test_jpx_parse_delisting_warning_html_rows_extracts_codes(self) -> None:
        provider = JPXProvider(Path("/tmp"))

        rows = provider._parse_html_rows(
            "上場廃止警告",
            self._read_jpx_fixture("delisting_warning.html"),
            "https://www.jpx.co.jp/listing/stocks/delisted/",
        )

        self.assertEqual(
            rows,
            [
                {"code": "7709", "flag": "上場廃止警告"},
                {"code": "7426", "flag": "上場廃止警告"},
            ],
        )

    def test_jpx_parse_html_rows_fail_fast_on_layout_change(self) -> None:
        provider = JPXProvider(Path("/tmp"))
        broken_html = self._read_jpx_fixture("reorganization.html").decode("utf-8").replace("整理銘柄", "整理銘柄一覧")

        with self.assertRaisesRegex(JPXProviderError, "failed to locate JPX HTML table"):
            provider._parse_html_rows(
                "整理銘柄",
                broken_html.encode("utf-8"),
                "https://www.jpx.co.jp/listing/market-alerts/supervision/",
            )

    def test_jpx_get_regulation_snapshot_fails_on_invalid_html_code(self) -> None:
        class FakeResponse:
            def __init__(self, content: bytes) -> None:
                self.content = content
                self.status_code = 200
                self.headers = {"content-type": "text/html; charset=UTF-8"}

        class FakeSession:
            def __init__(self, responses: dict[str, FakeResponse]) -> None:
                self._responses = responses

            def get(self, url: str, timeout: int) -> FakeResponse:
                del timeout
                return self._responses[url]

        url = "https://www.jpx.co.jp/markets/equities/suspended/"
        broken_html = self._read_jpx_fixture("trading_halt.html").decode("utf-8").replace("9941", "99411", 1).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            provider = JPXProvider(
                Path(tmp),
                regulation_urls={"取引停止": url},
                session=FakeSession({url: FakeResponse(broken_html)}),
            )
            with self.assertRaisesRegex(JPXProviderError, "invalid JPX code"):
                provider.get_regulation_snapshot(date(2026, 4, 24))

    def test_jpx_trading_halt_empty_marker_returns_no_rows(self) -> None:
        provider = JPXProvider(Path("/tmp"))
        rows = provider._parse_html_rows(
            "取引停止",
            self._read_jpx_fixture("trading_halt_empty.html"),
            "https://www.jpx.co.jp/markets/equities/suspended/",
        )
        self.assertEqual(rows, [])

    def test_jpx_trading_halt_flat_header_fails_fast(self) -> None:
        # ヘッダが 1 段になった場合、header_rows=2 を要求しているので 2 行目 (data) が
        # `<th>` 全列ではなく fail-fast に落ちる。これにより JPX が rowspan を外して
        # 出してきても列ズレで silently 1 件欠損する事故を防げる。
        provider = JPXProvider(Path("/tmp"))
        with self.assertRaisesRegex(JPXProviderError, "unexpected JPX HTML header layout"):
            provider._parse_html_rows(
                "取引停止",
                self._read_jpx_fixture("trading_halt_flat_header.html"),
                "https://www.jpx.co.jp/markets/equities/suspended/",
            )

    def test_jpx_reorganization_nested_table_uses_outer_table_only(self) -> None:
        # 外側 table.fixedhead の data 行に nested table が入っているケース。
        # HTMLParser の stack で外側のみ index される設計が壊れたら、内側の行が
        # 外側 row として混入し header check で raise されるか、コード列に
        # 別の値が入って test 期待値と不一致になる。
        provider = JPXProvider(Path("/tmp"))
        rows = provider._parse_html_rows(
            "整理銘柄",
            self._read_jpx_fixture("reorganization_nested.html"),
            "https://www.jpx.co.jp/listing/market-alerts/supervision/",
        )
        self.assertEqual(rows, [{"code": "4917", "flag": "整理銘柄"}])

    def test_jpx_download_rows_rejects_unsupported_format(self) -> None:
        class FakeResponse:
            content = b"<plain>"
            status_code = 200
            headers: dict[str, str] = {"content-type": "text/plain; charset=UTF-8"}

        class FakeSession:
            def get(self, url: str, timeout: int) -> FakeResponse:
                del url, timeout
                return FakeResponse()

        provider = JPXProvider(Path("/tmp"), session=FakeSession())
        with self.assertRaisesRegex(JPXProviderError, "unsupported JPX regulation source format"):
            provider._download_rows(
                "整理銘柄",
                "https://www.jpx.co.jp/listing/market-alerts/supervision/notes",
            )

    def test_jpx_download_rows_raises_on_http_error_status(self) -> None:
        class FakeResponse:
            content = b""
            status_code = 503
            headers: dict[str, str] = {}

        class FakeSession:
            def get(self, url: str, timeout: int) -> FakeResponse:
                del url, timeout
                return FakeResponse()

        provider = JPXProvider(Path("/tmp"), session=FakeSession())
        with self.assertRaisesRegex(JPXProviderError, "status=503"):
            provider._download_rows(
                "整理銘柄",
                "https://www.jpx.co.jp/listing/market-alerts/supervision/",
            )

    def test_jpx_get_regulation_snapshot_uses_cache_without_calling_session(self) -> None:
        class ExplodingSession:
            def get(self, url: str, timeout: int):
                del url, timeout
                raise AssertionError("session.get must not be called when cache is present")

        url = "https://www.jpx.co.jp/listing/market-alerts/supervision/"
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            cache_path = cache_dir / "jpx" / "regulations" / "2026-04-24.json"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                '{"schema_version": 1, "flags_by_ticker": {"4917": ["整理銘柄"]}, "source_names": ["整理銘柄"]}',
                encoding="utf-8",
            )

            provider = JPXProvider(
                cache_dir,
                regulation_urls={"整理銘柄": url},
                session=ExplodingSession(),
            )
            snapshot = provider.get_regulation_snapshot(date(2026, 4, 24))

        self.assertEqual(snapshot.source_names, ("整理銘柄",))
        self.assertEqual(snapshot.flags_by_ticker, {"4917": ("整理銘柄",)})

    def test_jpx_get_regulation_snapshot_records_fetched_at_utc_on_fetch(self) -> None:
        class FakeResponse:
            status_code = 200
            content = "code\n49170\n".encode("utf-8")
            headers = {"content-type": "text/csv"}

        class FakeSession:
            def get(self, url: str, timeout: int) -> FakeResponse:
                del url, timeout
                return FakeResponse()

        url = "https://www.jpx.co.jp/listing/market-alerts/supervision/list.csv"
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            provider = JPXProvider(
                cache_dir,
                regulation_urls={"整理銘柄": url},
                session=FakeSession(),
            )
            provider.get_regulation_snapshot(date(2026, 4, 24))
            cache_path = cache_dir / "jpx" / "regulations" / "2026-04-24.json"
            payload = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema_version"], 1)
        self.assertIn("fetched_at_utc", payload)
        self.assertEqual(payload["flags_by_ticker"], {"4917": ["整理銘柄"]})

    def test_jpx_get_regulation_snapshot_reads_legacy_cache_without_schema_version(self) -> None:
        url = "https://www.jpx.co.jp/listing/market-alerts/supervision/"
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            cache_path = cache_dir / "jpx" / "regulations" / "2026-04-24.json"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                '{"flags_by_ticker": {"4917": ["整理銘柄"]}, "source_names": ["整理銘柄"]}',
                encoding="utf-8",
            )

            provider = JPXProvider(cache_dir, regulation_urls={"整理銘柄": url})
            snapshot = provider.get_regulation_snapshot(date(2026, 4, 24))

        self.assertEqual(snapshot.flags_by_ticker, {"4917": ("整理銘柄",)})

    def test_jpx_get_regulation_snapshot_reads_cache_without_fetched_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            cache_path = cache_dir / "jpx" / "regulations" / "2026-04-24.json"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                '{"schema_version": 1, "flags_by_ticker": {"4917": ["整理銘柄"]}, "source_names": ["整理銘柄"]}',
                encoding="utf-8",
            )

            provider = JPXProvider(cache_dir)
            snapshot = provider.get_regulation_snapshot(date(2026, 4, 24))

        self.assertEqual(snapshot.flags_by_ticker, {"4917": ("整理銘柄",)})

    def test_jpx_get_regulation_snapshot_warns_when_cache_fetch_date_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            cache_path = cache_dir / "jpx" / "regulations" / "2026-01-15.json"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "fetched_at_utc": "2026-04-24T00:00:00+00:00",
                        "flags_by_ticker": {"4917": ["整理銘柄"]},
                        "source_names": ["整理銘柄"],
                    }
                ),
                encoding="utf-8",
            )

            provider = JPXProvider(cache_dir)
            with self.assertLogs("baibai_loop.screening.providers.jpx", level="WARNING") as logs:
                snapshot = provider.get_regulation_snapshot(date(2026, 1, 15))

        self.assertEqual(snapshot.flags_by_ticker, {"4917": ("整理銘柄",)})
        self.assertIn("JPX regulation cache may be stale", "\n".join(logs.output))

    def test_jpx_get_regulation_snapshot_warns_just_over_stale_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            cache_path = cache_dir / "jpx" / "regulations" / "2026-04-14.json"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "fetched_at_utc": "2026-04-24T00:00:00+00:00",
                        "flags_by_ticker": {"4917": ["整理銘柄"]},
                        "source_names": ["整理銘柄"],
                    }
                ),
                encoding="utf-8",
            )

            provider = JPXProvider(cache_dir)
            with self.assertLogs("baibai_loop.screening.providers.jpx", level="WARNING") as logs:
                snapshot = provider.get_regulation_snapshot(date(2026, 4, 14))

        self.assertEqual(snapshot.flags_by_ticker, {"4917": ("整理銘柄",)})
        self.assertIn("weekdays=8", "\n".join(logs.output))

    def test_jpx_get_regulation_snapshot_does_not_warn_at_stale_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            cache_path = cache_dir / "jpx" / "regulations" / "2026-04-15.json"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "fetched_at_utc": "2026-04-24T00:00:00+00:00",
                        "flags_by_ticker": {"4917": ["整理銘柄"]},
                        "source_names": ["整理銘柄"],
                    }
                ),
                encoding="utf-8",
            )

            provider = JPXProvider(cache_dir)
            with self.assertNoLogs("baibai_loop.screening.providers.jpx", level="WARNING"):
                snapshot = provider.get_regulation_snapshot(date(2026, 4, 15))

        self.assertEqual(snapshot.flags_by_ticker, {"4917": ("整理銘柄",)})

    def test_jpx_get_regulation_snapshot_warns_on_invalid_fetched_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            cache_path = cache_dir / "jpx" / "regulations" / "2026-04-24.json"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "fetched_at_utc": "not-a-date",
                        "flags_by_ticker": {"4917": ["整理銘柄"]},
                        "source_names": ["整理銘柄"],
                    }
                ),
                encoding="utf-8",
            )

            provider = JPXProvider(cache_dir)
            with self.assertLogs("baibai_loop.screening.providers.jpx", level="WARNING") as logs:
                snapshot = provider.get_regulation_snapshot(date(2026, 4, 24))

        self.assertEqual(snapshot.flags_by_ticker, {"4917": ("整理銘柄",)})
        self.assertIn("invalid fetched_at_utc", "\n".join(logs.output))

    def test_jpx_get_regulation_snapshot_rejects_incompatible_cache_schema(self) -> None:
        url = "https://www.jpx.co.jp/listing/market-alerts/supervision/"
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            cache_path = cache_dir / "jpx" / "regulations" / "2026-04-24.json"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                '{"schema_version": 99, "flags_by_ticker": {}, "source_names": []}',
                encoding="utf-8",
            )

            provider = JPXProvider(cache_dir, regulation_urls={"整理銘柄": url})
            with self.assertRaisesRegex(JPXProviderError, "incompatible JPX cache schema version"):
                provider.get_regulation_snapshot(date(2026, 4, 24))

    def test_jpx_decode_html_text_prefers_content_type_charset(self) -> None:
        # JPX が cp932 ページを Content-Type で告知してきた場合、フォールバックの utf-8
        # が偶然成功して文字化けが残るリスクを避けるため、charset を最優先で試行する。
        provider = JPXProvider(Path("/tmp"))
        cp932_payload = "整理銘柄".encode("cp932")
        decoded = provider._decode_html_text(
            cp932_payload,
            "https://www.jpx.co.jp/listing/market-alerts/supervision/",
            "text/html; charset=Shift_JIS",
        )
        self.assertEqual(decoded, "整理銘柄")

    def test_jpx_decode_html_text_raises_when_all_encodings_fail(self) -> None:
        provider = JPXProvider(Path("/tmp"))
        # 0xFF は cp932 / utf-8 / utf-8-sig いずれでも lead byte として無効なので、
        # フォールバック 3 段すべて UnicodeDecodeError で落ちる。LookupError 経路の
        # 確認も兼ねて、Content-Type には未知の encoding 名を載せる。
        with self.assertRaises(JPXProviderError):
            provider._decode_html_text(
                b"\x81\x00\x81\x00",
                "https://www.jpx.co.jp/listing/market-alerts/supervision/",
                "text/html; charset=jpx-unknown",
            )

    def test_jpx_reorganization_invalid_code_raises(self) -> None:
        broken_html = (
            self._read_jpx_fixture("reorganization.html")
            .decode("utf-8")
            .replace("4917", "49171", 1)
            .encode("utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp:
            provider = JPXProvider(
                Path(tmp),
                regulation_urls={
                    "整理銘柄": "https://www.jpx.co.jp/listing/market-alerts/supervision/"
                },
                session=_FixedHtmlSession(broken_html),
            )
            with self.assertRaisesRegex(JPXProviderError, "invalid JPX code"):
                provider.get_regulation_snapshot(date(2026, 4, 24))

    def test_jpx_delisting_warning_invalid_code_raises(self) -> None:
        broken_html = (
            self._read_jpx_fixture("delisting_warning.html")
            .decode("utf-8")
            .replace("7709", "77091", 1)
            .encode("utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp:
            provider = JPXProvider(
                Path(tmp),
                regulation_urls={
                    "上場廃止警告": "https://www.jpx.co.jp/listing/stocks/delisted/"
                },
                session=_FixedHtmlSession(broken_html),
            )
            with self.assertRaisesRegex(JPXProviderError, "invalid JPX code"):
                provider.get_regulation_snapshot(date(2026, 4, 24))
