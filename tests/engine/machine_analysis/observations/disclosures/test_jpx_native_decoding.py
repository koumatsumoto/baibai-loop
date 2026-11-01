"""現行JPX開示のnative投影の回帰契約。"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date
from io import BytesIO
from itertools import permutations
from pathlib import Path

import pandas as pd

from baibai_engine.machine_analysis.observations.disclosures.jpx_decode import (
    _decode_html_text,
    _EarningsCalendarFile,
    _parse_special_attention_html_rows,
    build_earnings_calendar_snapshot,
    build_regulation_snapshot,
    parse_earnings_calendar_excel,
    parse_regulation_rows,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_models import (
    JPXEarningsCalendarEntry,
    JPXEarningsCalendarSnapshot,
    JPXProviderError,
)

ROOT = Path(__file__).resolve().parents[5]


class JPXNativeDecodingTests(unittest.TestCase):
    def _read_jpx_fixture(self, name: str) -> bytes:
        return (ROOT / "tests" / "fixtures" / "jpx" / name).read_bytes()

    @staticmethod
    def _earnings_xlsx(rows: list[list[str]]) -> bytes:
        buffer = BytesIO()
        pd.DataFrame(rows).to_excel(buffer, index=False, header=False)
        return buffer.getvalue()

    @staticmethod
    def _earnings_url(cohort_suffix: str = "") -> str:
        return (
            "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/"
            f"att/kessan{cohort_suffix}.xlsx"
        )

    @staticmethod
    def _earnings_file(
        url: str,
        published_on: date | None,
        dates_by_ticker: dict[str, date],
        *,
        prefix: str = "att",
    ) -> _EarningsCalendarFile:
        return _EarningsCalendarFile(
            url=url.replace("/att/", f"/{prefix}/"),
            published_on=published_on,
            entries=tuple(
                JPXEarningsCalendarEntry(ticker=ticker, announcement_date=on_date)
                for ticker, on_date in dates_by_ticker.items()
            ),
            raw_record_count=len(dates_by_ticker),
            excluded_record_count=0,
        )

    def test_jpx_earnings_parser_detects_header_and_normalizes_rows(self) -> None:
        content = self._earnings_xlsx(
            [
                ["決算発表予定", "", ""],
                [
                    "決算発表予定日\nScheduled Dates for Earnings Announcements",
                    "コード\nCode",
                    "会社名\nCompany Name",
                ],
                ["2026-07-15", "130A", "Alpha"],
                ["未定_Undecided", "7203", "Toyota"],
                ["", "※注", ""],
                ["2026/07/16", "72030", "Toyota"],
            ]
        )

        parsed = parse_earnings_calendar_excel(
            content,
            "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/kessan.xlsx",
        )

        self.assertEqual(
            [(entry.ticker, entry.announcement_date) for entry in parsed.entries],
            [("130A", date(2026, 7, 15))],
        )
        self.assertEqual(parsed.raw_record_count, 3)
        self.assertEqual(parsed.excluded_record_count, 2)
        self.assertEqual(parsed.undated_tickers, ("7203",))
        self.assertIsNone(parsed.published_on)

    def test_jpx_earnings_parser_fails_on_layout_and_invalid_code(self) -> None:
        url = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/kessan.xlsx"
        with self.assertRaisesRegex(JPXProviderError, "header layout"):
            parse_earnings_calendar_excel(
                self._earnings_xlsx([["Date", "Code"], ["2026-07-15", "130A"]]), url
            )
        with self.assertRaisesRegex(JPXProviderError, "invalid JPX code"):
            parse_earnings_calendar_excel(
                self._earnings_xlsx([["決算発表日", "コード"], ["2026-07-15", "INVALID"]]),
                url,
            )

    def test_jpx_earnings_snapshot_fails_when_every_date_is_past(self) -> None:
        url = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/a/kessan.xlsx"
        with self.assertRaisesRegex(JPXProviderError, "only past dates"):
            build_earnings_calendar_snapshot(
                (self._earnings_file(url, None, {"130A": date(2026, 7, 13)}),),
                asof_date=date(2026, 7, 14),
                source_urls=(url,),
            )

    def _merge_earnings_files(
        self, files: tuple[_EarningsCalendarFile, ...], asof_date: date
    ) -> JPXEarningsCalendarSnapshot:
        by_url = {source.url: source for source in files}
        snapshots = [
            build_earnings_calendar_snapshot(
                tuple(by_url[url] for url in urls), asof_date=asof_date, source_urls=urls
            )
            for urls in permutations(by_url)
        ]
        canonical = replace(snapshots[0], source_urls=tuple(sorted(snapshots[0].source_urls)))
        for snapshot in snapshots[1:]:
            self.assertEqual(
                canonical, replace(snapshot, source_urls=tuple(sorted(snapshot.source_urls)))
            )
        return snapshots[0]

    def test_jpx_earnings_snapshot_keeps_the_freshest_publication_on_conflict(self) -> None:
        snapshot = self._merge_earnings_files(
            (
                self._earnings_file(self._earnings_url(), None, {"130A": date(2026, 7, 15)}),
                self._earnings_file(
                    self._earnings_url("06_0731"),
                    date(2026, 7, 30),
                    {"130A": date(2026, 7, 16), "7203": date(2026, 7, 21)},
                ),
                self._earnings_file(
                    self._earnings_url("05_0703"),
                    date(2026, 7, 2),
                    {"7203": date(2026, 7, 22), "6758": date(2026, 7, 23)},
                ),
            ),
            date(2026, 7, 14),
        )

        self.assertEqual(
            [(entry.ticker, entry.announcement_date) for entry in snapshot.entries],
            [
                ("130A", date(2026, 7, 15)),
                ("7203", date(2026, 7, 21)),
                ("6758", date(2026, 7, 23)),
            ],
        )
        self.assertEqual(snapshot.superseded_record_count, 2)
        self.assertEqual(snapshot.raw_record_count, 5)

    def test_jpx_earnings_snapshot_keeps_the_rolling_file_above_an_unstamped_archive(self) -> None:
        # A stamp JPX stops writing in a form we parse must not promote the
        # archive to "current"; precedence identifies the rolling file by name.
        snapshot = self._merge_earnings_files(
            (
                self._earnings_file(self._earnings_url(), None, {"130A": date(2026, 7, 15)}),
                self._earnings_file(
                    self._earnings_url("06_0731"),
                    None,
                    {"130A": date(2026, 7, 16)},
                    # Sorts ahead of the rolling URL, so only the rolling rank can
                    # be what puts the rolling date on top.
                    prefix="a",
                ),
            ),
            date(2026, 7, 14),
        )

        self.assertEqual(snapshot.entries[0].announcement_date, date(2026, 7, 15))

    def test_jpx_earnings_snapshot_keeps_a_dated_archive_above_an_unstamped_one(self) -> None:
        snapshot = self._merge_earnings_files(
            (
                self._earnings_file(
                    self._earnings_url("06_0731"), date(2026, 7, 30), {"130A": date(2026, 7, 15)}
                ),
                self._earnings_file(
                    self._earnings_url("05_0703"), None, {"130A": date(2026, 7, 16)}
                ),
            ),
            date(2026, 7, 14),
        )

        self.assertEqual(snapshot.entries[0].announcement_date, date(2026, 7, 15))

    def test_jpx_earnings_snapshot_ignores_a_stale_file_with_no_forward_dates(self) -> None:
        # A rolling file JPX failed to refresh holds only past dates and must not
        # blank out a still-scheduled announcement.
        snapshot = self._merge_earnings_files(
            (
                self._earnings_file(self._earnings_url(), None, {"130A": date(2026, 7, 10)}),
                self._earnings_file(
                    self._earnings_url("06_0731"), date(2026, 7, 9), {"130A": date(2026, 7, 20)}
                ),
            ),
            date(2026, 7, 14),
        )

        self.assertEqual(snapshot.entries[0].announcement_date, date(2026, 7, 20))

    def test_jpx_earnings_snapshot_keeps_equal_priority_conflicts_unknown(self) -> None:
        snapshot = self._merge_earnings_files(
            (
                self._earnings_file(
                    self._earnings_url("06_0715"), date(2026, 7, 15), {"130A": date(2026, 7, 20)}
                ),
                self._earnings_file(
                    self._earnings_url("07_0715"),
                    date(2026, 7, 15),
                    {"130A": date(2026, 7, 25), "7203": date(2026, 7, 26)},
                ),
            ),
            date(2026, 7, 14),
        )
        self.assertEqual(
            [(entry.ticker, entry.announcement_date) for entry in snapshot.entries],
            [("7203", date(2026, 7, 26))],
        )

    def test_jpx_earnings_snapshot_warns_when_archive_publication_stamp_is_missing(self) -> None:
        files = (
            self._earnings_file(
                self._earnings_url("06_0731"), None, {"130A": date(2026, 7, 20)}, prefix="att"
            ),
            self._earnings_file(
                self._earnings_url("06_0731"), None, {"7203": date(2026, 7, 21)}, prefix="att2"
            ),
        )
        with self.assertLogs(
            "baibai_engine.machine_analysis.observations.disclosures.jpx_decode", level="WARNING"
        ) as logs:
            build_earnings_calendar_snapshot(
                files, asof_date=date(2026, 7, 14), source_urls=tuple(f.url for f in files)
            )
        self.assertIn("no readable publication stamp", "\n".join(logs.output))

    def test_jpx_earnings_snapshot_without_daily_file_is_not_a_layout_warning(self) -> None:
        files = (
            self._earnings_file(
                self._earnings_url("09_1009"), date(2026, 10, 8), {"130A": date(2026, 10, 20)}
            ),
        )
        with self.assertNoLogs(
            "baibai_engine.machine_analysis.observations.disclosures.jpx_decode", level="WARNING"
        ):
            snapshot = self._merge_earnings_files(files, date(2026, 10, 15))
        self.assertEqual(snapshot.entries[0].announcement_date, date(2026, 10, 20))

    def test_jpx_earnings_snapshot_warns_when_several_rolling_files_are_listed(self) -> None:
        files = (
            self._earnings_file(
                self._earnings_url(), None, {"130A": date(2026, 7, 20)}, prefix="att"
            ),
            self._earnings_file(
                self._earnings_url(), None, {"7203": date(2026, 7, 21)}, prefix="att2"
            ),
        )
        with self.assertLogs(
            "baibai_engine.machine_analysis.observations.disclosures.jpx_decode", level="WARNING"
        ) as logs:
            build_earnings_calendar_snapshot(
                files, asof_date=date(2026, 7, 14), source_urls=tuple(f.url for f in files)
            )
        self.assertIn("several rolling `kessan` files", "\n".join(logs.output))

    def test_jpx_earnings_parser_reads_publication_stamp_but_not_cohort_titles(self) -> None:
        url = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/kessan.xlsx"
        header = ["決算発表予定日", "コード"]

        stamped = parse_earnings_calendar_excel(
            self._earnings_xlsx(
                [
                    ["2027年3月期第1四半期決算（2026年8月6日開示予定分）", ""],
                    ["2026年7月30日 現在", ""],
                    header,
                    ["2026-08-07", "130A"],
                ]
            ),
            url,
        )
        self.assertEqual(stamped.published_on, date(2026, 7, 30))

        english_only = parse_earnings_calendar_excel(
            self._earnings_xlsx([["As of 2026/7/2", ""], header, ["2026-08-07", "130A"]]),
            url,
        )
        self.assertEqual(english_only.published_on, date(2026, 7, 2))

        untitled = parse_earnings_calendar_excel(
            self._earnings_xlsx(
                [
                    ["６月に四半期末を迎えた決算発表予定会社の一覧", ""],
                    header,
                    ["2026-08-07", "130A"],
                ]
            ),
            url,
        )
        self.assertIsNone(untitled.published_on)

        # An impossible date in one cell must not hide a readable stamp beside it.
        beside_a_bad_cell = parse_earnings_calendar_excel(
            self._earnings_xlsx(
                [["2026年13月40日 現在", "As of 2026/7/2"], header, ["2026-08-07", "130A"]]
            ),
            url,
        )
        self.assertEqual(beside_a_bad_cell.published_on, date(2026, 7, 2))

    def test_jpx_earnings_parser_drops_a_ticker_dated_twice_and_keeps_the_file(self) -> None:
        """2026-08-05: one issuer with two dates stopped the whole fetch, and the day.

        The ticker leaves as undated — the same exit a 未定 row takes — and every
        other issuer in the file is still delivered. A cell that is not a date at
        all takes the same exit.
        """

        with self.assertLogs(
            "baibai_engine.machine_analysis.observations.disclosures.jpx_decode", level="WARNING"
        ) as logs:
            parsed = parse_earnings_calendar_excel(
                self._earnings_xlsx(
                    [
                        ["決算発表予定日", "コード"],
                        ["2026-08-06", "130A"],
                        ["2026-08-07", "130A"],
                        ["2026-08-07", "1301"],
                        ["2026-08-08", "130A"],
                        ["8月上旬", "1305"],
                    ]
                ),
                "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/k.xlsx",
            )

        self.assertEqual(
            parsed.entries,
            (JPXEarningsCalendarEntry(ticker="1301", announcement_date=date(2026, 8, 7)),),
        )
        self.assertEqual(parsed.raw_record_count, 5)
        self.assertEqual(parsed.excluded_record_count, 4)
        self.assertIn("conflicting=1 unparsable=1", logs.output[0])
        self.assertIn("1305: '8月上旬'", logs.output[0])
        self.assertIn("130A: 2026-08-06 and 2026-08-07 and 2026-08-08", logs.output[0])

    def test_jpx_special_attention_current_sections(self) -> None:
        html = self._read_jpx_fixture("special_caution.html")
        snapshot = build_regulation_snapshot(
            {
                "特別注意銘柄": parse_regulation_rows(
                    "特別注意銘柄",
                    html,
                    "https://www.jpx.co.jp/listing/measures/alert/index.html",
                )
            }
        )
        self.assertEqual(
            snapshot.flags_by_ticker, {"130A": ("特別注意銘柄",), "2901": ("特別注意銘柄",)}
        )
        self.assertEqual(snapshot.source_names, ("特別注意銘柄",))

    def test_jpx_special_attention_merges_and_deduplicates_sections(self) -> None:
        html = self._read_jpx_fixture("special_caution.html").decode()
        html = html.replace(
            "<p>現在、該当する情報はありません。</p>",
            "<table><tr><th>銘柄名</th><th>コード</th></tr>"
            "<tr><td>A</td><td>29010</td></tr><tr><td>C</td><td>6548</td></tr></table>",
        )
        rows = _parse_special_attention_html_rows(html, "fixture")
        self.assertEqual(
            rows,
            [{"code": code, "flag": "特別注意銘柄"} for code in ("2901", "130A", "6548")],
        )

    def test_jpx_special_attention_all_sections_empty(self) -> None:
        html = "<h2>現在の指定状況</h2><p>現在、該当する情報はありません。</p>"
        self.assertEqual(_parse_special_attention_html_rows(html, "fixture"), [])

    def test_jpx_special_attention_rejects_layout_drift(self) -> None:
        html = self._read_jpx_fixture("special_caution.html").decode()
        cases = {
            "missing code header": html.replace("コード", "識別子"),
            "short code row": html.replace('<td rowspan="2">2901</td>', ""),
            "missing continuation": html.replace("<tr><td>-</td><td>-</td></tr>", "", 1),
            "broken continuation": html.replace(
                "<tr><td>-</td><td>-</td></tr>", "<tr><td>-</td></tr>"
            ),
            "changed rowspan": html.replace('rowspan="2">2901', 'rowspan="3">2901'),
            "changed colspan": html.replace('rowspan="2">2901', 'rowspan="2" colspan="2">2901'),
            "invalid code": html.replace(">2901<", ">invalid<"),
            "missing section content": html.replace("<p>現在、該当する情報はありません。</p>", ""),
            "unrelated table": "<h2>指定履歴</h2><table><tr><th>コード</th></tr><tr><td>9999</td></tr></table>",
            "unrelated empty marker": "<h2>現在の指定状況</h2><h2>指定履歴</h2><p>現在、該当する情報はありません。</p>",
            "header only": "<h2>現在の指定状況</h2><table><tr><th>コード</th></tr></table>",
        }
        for label, changed in cases.items():
            with self.subTest(label=label), self.assertRaises(JPXProviderError):
                _parse_special_attention_html_rows(changed, "fixture")

    def test_jpx_parse_reorganization_html_rows_from_content_type(self) -> None:
        rows = parse_regulation_rows(
            "整理銘柄",
            self._read_jpx_fixture("reorganization.html"),
            "https://www.jpx.co.jp/listing/market-alerts/supervision/",
            "text/html; charset=UTF-8",
        )
        self.assertEqual(
            rows,
            [
                {"code": "4917", "flag": "整理銘柄"},
                {"code": "7426", "flag": "整理銘柄"},
            ],
        )

    def test_jpx_parse_trading_halt_html_rows_extracts_codes(self) -> None:

        rows = parse_regulation_rows(
            "取引停止",
            self._read_jpx_fixture("trading_halt.html"),
            "https://www.jpx.co.jp/markets/equities/suspended/",
        )

        self.assertEqual(rows, [{"code": "9941", "flag": "取引停止"}])

    def test_jpx_parse_delisting_warning_html_rows_extracts_codes(self) -> None:

        rows = parse_regulation_rows(
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
        broken_html = (
            self._read_jpx_fixture("reorganization.html")
            .decode("utf-8")
            .replace("整理銘柄", "整理銘柄一覧")
        )

        with self.assertRaisesRegex(JPXProviderError, "failed to locate JPX HTML table"):
            parse_regulation_rows(
                "整理銘柄",
                broken_html.encode("utf-8"),
                "https://www.jpx.co.jp/listing/market-alerts/supervision/",
            )

    def test_jpx_get_regulation_snapshot_fails_on_invalid_html_code(self) -> None:
        broken_html = (
            self._read_jpx_fixture("trading_halt.html")
            .decode("utf-8")
            .replace("9941", "99411", 1)
            .encode("utf-8")
        )
        with self.assertRaisesRegex(JPXProviderError, "invalid JPX code"):
            build_regulation_snapshot(
                {
                    "取引停止": parse_regulation_rows(
                        "取引停止", broken_html, "https://www.jpx.co.jp/markets/equities/suspended/"
                    )
                }
            )

    def test_jpx_trading_halt_empty_marker_returns_no_rows(self) -> None:
        rows = parse_regulation_rows(
            "取引停止",
            self._read_jpx_fixture("trading_halt_empty.html"),
            "https://www.jpx.co.jp/markets/equities/suspended/",
        )
        self.assertEqual(rows, [])

    def test_jpx_trading_halt_flat_header_fails_fast(self) -> None:
        # ヘッダが 1 段になった場合、header_rows=2 を要求しているので 2 行目 (data) が
        # `<th>` 全列ではなく fail-fast に落ちる。これにより JPX が rowspan を外して
        # 出してきても列ズレで silently 1 件欠損する事故を防げる。
        with self.assertRaisesRegex(JPXProviderError, "unexpected JPX HTML header layout"):
            parse_regulation_rows(
                "取引停止",
                self._read_jpx_fixture("trading_halt_flat_header.html"),
                "https://www.jpx.co.jp/markets/equities/suspended/",
            )

    def test_jpx_reorganization_nested_table_uses_outer_table_only(self) -> None:
        # 外側 table.fixedhead の data 行に nested table が入っているケース。
        # HTMLParser の stack で外側のみ index される設計が壊れたら、内側の行が
        # 外側 row として混入し header check で raise されるか、コード列に
        # 別の値が入って test 期待値と不一致になる。
        rows = parse_regulation_rows(
            "整理銘柄",
            self._read_jpx_fixture("reorganization_nested.html"),
            "https://www.jpx.co.jp/listing/market-alerts/supervision/",
        )
        self.assertEqual(rows, [{"code": "4917", "flag": "整理銘柄"}])

    def test_jpx_decode_html_text_prefers_content_type_charset(self) -> None:
        # JPX が cp932 ページを Content-Type で告知してきた場合、フォールバックの utf-8
        # が偶然成功して文字化けが残るリスクを避けるため、charset を最優先で試行する。
        cp932_payload = "整理銘柄".encode("cp932")
        decoded = _decode_html_text(
            cp932_payload,
            "https://www.jpx.co.jp/listing/market-alerts/supervision/",
            "text/html; charset=Shift_JIS",
        )
        self.assertEqual(decoded, "整理銘柄")

    def test_jpx_decode_html_text_raises_when_all_encodings_fail(self) -> None:
        # 0x81 に続く 0x00 は cp932 の有効なバイト列ではなく、utf-8 / utf-8-sig
        # でも decode できない。LookupError 経路の
        # 確認も兼ねて、Content-Type には未知の encoding 名を載せる。
        with self.assertRaises(JPXProviderError):
            _decode_html_text(
                b"\x81\x00\x81\x00",
                "https://www.jpx.co.jp/listing/market-alerts/supervision/",
                "text/html; charset=jpx-unknown",
            )

    def test_jpx_refuses_an_invalid_code_in_every_regulation_page(self) -> None:
        cases = (
            (
                "整理銘柄",
                "reorganization.html",
                b"4917",
                b"49171",
                "https://www.jpx.co.jp/listing/market-alerts/supervision/",
            ),
            (
                "上場廃止警告",
                "delisting_warning.html",
                b"7709",
                b"77091",
                "https://www.jpx.co.jp/listing/stocks/delisted/",
            ),
        )
        for name, fixture, code, broken, url in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(JPXProviderError, "invalid JPX code"),
            ):
                build_regulation_snapshot(
                    {
                        name: parse_regulation_rows(
                            name, self._read_jpx_fixture(fixture).replace(code, broken, 1), url
                        )
                    }
                )
