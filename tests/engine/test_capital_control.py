from __future__ import annotations

import unittest
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

import openpyxl

from baibai_engine.market.sqlite import connect_current
from baibai_engine.screening.capital_control import (
    CapitalControlError,
    JPXDelistingRow,
    _download_jpx_delistings,
    _TableLinkParser,
    edinet_identity_covered,
    parse_tse_capital_policy_workbook,
    read_capital_control_annotations,
    read_control_event_index,
    read_jpx_delistings,
    store_jpx_delistings,
    store_tse_capital_policy_rows,
)
from baibai_engine.screening.sqlite_cache import open_connection


# The two real layouts. The 2025 sheets put the contact application next to the update
# date; the 2026 sheets inserted the disclosure text between them, which is what makes a
# fixed column index read free-form prose as a contact request.
def _naive_datetime(year: int, month: int, day: int) -> datetime:
    """openpyxl writes cell dates without a zone, which is what the reader receives."""
    return datetime(year, month, day)  # noqa: DTZ001


_GROUP_2025 = (
    None,
    "業種\nコード",
    "業種",
    "市場区分",
    "証券\nコード",
    "銘柄名",
    "開示状況",
    None,
    "開示内容の\nアップデート日",
    "機関投資家からのより活発なコンタクトを希望",
    None,
    None,
    "英文開示",
)
_GROUP_2026 = (
    None,
    "業種\nコード",
    "業種",
    "市場区分",
    "証券\nコード",
    "銘柄名",
    "開示状況",
    None,
    "開示内容の\nアップデート日",
    "開示内容 ※１",
    "機関投資家からのより活発なコンタクトを希望 ※２",
    None,
    None,
    "英文開示",
)
_DETAIL_2025 = (
    None,
    None,
    None,
    None,
    None,
    None,
    "要請に基づく\n開示状況",
    "前月からの\n開示状況の変更",
    None,
    "申請状況",
    "掲載開始日",
    "コンタクト先",
    None,
)
_DETAIL_2026 = (
    None,
    None,
    None,
    None,
    None,
    None,
    "要請に基づく\n開示状況",
    "前月からの\n開示状況の変更",
    None,
    None,
    "申請状況",
    "掲載開始日",
    "コンタクト先",
    None,
)


def _workbook(
    sheets: list[tuple[str, tuple[object, ...], tuple[object, ...], list[tuple]]],
) -> bytes:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for title, group, detail, rows in sheets:
        sheet = workbook.create_sheet(title=title)
        sheet.append(("本資料の目的",))
        sheet.append(group)
        sheet.append(detail)
        for row in rows:
            sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _row_2026(ticker: int, status: str, *, updated: object = "", contact: str = "") -> tuple:
    return (
        None,
        50,
        "水産・農林業",
        "プライム",
        ticker,
        "極洋",
        status,
        "",
        updated,
        "本文",
        contact,
        "",
        "",
        "有",
    )


def _row_2025(ticker: int, status: str, *, updated: object = "", contact: str = "") -> tuple:
    return (
        None,
        50,
        "水産・農林業",
        "プライム",
        ticker,
        "極洋",
        status,
        "",
        updated,
        contact,
        "",
        "",
        "有",
    )


class TSECapitalPolicyWorkbookTest(unittest.TestCase):
    def test_columns_are_resolved_per_sheet_rather_than_by_position(self) -> None:
        content = _workbook(
            [
                (
                    "開示企業一覧（2026年6月末時点）",
                    _GROUP_2026,
                    _DETAIL_2026,
                    [_row_2026(1301, "開示済", updated=_naive_datetime(2026, 6, 23), contact="○")],
                ),
                (
                    "【過去分】開示企業一覧（2025年5月末時点）",
                    _GROUP_2025,
                    _DETAIL_2025,
                    [_row_2025(1301, "検討中")],
                ),
            ]
        )

        rows = {row.snapshot_month_end: row for row in parse_tse_capital_policy_workbook(content)}

        latest = rows[date(2026, 6, 30)]
        self.assertEqual(latest.status, "disclosed")
        self.assertEqual(latest.updated_on, date(2026, 6, 23))
        self.assertTrue(latest.contact_requested)
        earliest = rows[date(2025, 5, 31)]
        self.assertEqual(earliest.status, "considering")
        self.assertFalse(earliest.contact_requested)

    def test_disclosure_text_is_not_read_as_a_contact_request(self) -> None:
        content = _workbook(
            [
                (
                    "開示企業一覧（2026年6月末時点）",
                    _GROUP_2026,
                    _DETAIL_2026,
                    [_row_2026(1301, "開示済")],
                )
            ]
        )

        (row,) = parse_tse_capital_policy_workbook(content)

        self.assertFalse(row.contact_requested)

    def test_first_disclosure_on_the_earliest_sheet_is_left_censored(self) -> None:
        content = _workbook(
            [
                (
                    "開示企業一覧（2026年6月末時点）",
                    _GROUP_2026,
                    _DETAIL_2026,
                    [_row_2026(1301, "開示済"), _row_2026(1332, "開示済")],
                ),
                (
                    "【過去分】開示企業一覧（2025年5月末時点）",
                    _GROUP_2025,
                    _DETAIL_2025,
                    [_row_2025(1301, "開示済"), _row_2025(1332, "検討中")],
                ),
            ]
        )

        rows = {
            (row.ticker, row.snapshot_month_end): row
            for row in parse_tse_capital_policy_workbook(content)
        }

        already_listed = rows[("1301", date(2026, 6, 30))]
        self.assertEqual(already_listed.first_disclosed_month_end, date(2025, 5, 31))
        self.assertTrue(already_listed.first_disclosure_left_censored)
        newly_disclosed = rows[("1332", date(2026, 6, 30))]
        self.assertEqual(newly_disclosed.first_disclosed_month_end, date(2026, 6, 30))
        self.assertFalse(newly_disclosed.first_disclosure_left_censored)

    def test_an_unknown_status_word_is_refused_rather_than_guessed(self) -> None:
        content = _workbook(
            [
                (
                    "開示企業一覧（2026年6月末時点）",
                    _GROUP_2026,
                    _DETAIL_2026,
                    [_row_2026(1301, "未定")],
                )
            ]
        )

        with self.assertRaises(CapitalControlError):
            parse_tse_capital_policy_workbook(content)

    def test_a_sheet_without_the_typed_header_is_refused(self) -> None:
        content = _workbook(
            [
                (
                    "開示企業一覧（2026年6月末時点）",
                    (None, "銘柄", "市場"),
                    (None, None, None),
                    [(None, 1301, "プライム")],
                )
            ]
        )

        with self.assertRaises(CapitalControlError):
            parse_tse_capital_policy_workbook(content)

    def test_a_workbook_with_no_monthly_sheet_is_refused(self) -> None:
        content = _workbook([("本資料の目的", (None,), (None,), [])])

        with self.assertRaises(CapitalControlError):
            parse_tse_capital_policy_workbook(content)


class JPXDelistingTest(unittest.TestCase):
    def test_delistings_accumulate_across_refreshes(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            open_connection(sqlite_path).close()
            store_jpx_delistings(
                sqlite_path,
                [
                    JPXDelistingRow(
                        delisted_on=date(2024, 5, 1),
                        ticker="1000",
                        name="旧年の会社",
                        market="スタンダード",
                        reason="株式の併合",
                    )
                ],
            )

            # A later refresh reads a page that no longer lists the 2024 archive.
            store_jpx_delistings(
                sqlite_path,
                [
                    JPXDelistingRow(
                        delisted_on=date(2026, 5, 1),
                        ticker="2000",
                        name="今年の会社",
                        market="プライム",
                        reason="ＭＢＯ（公開買付け、株式併合）",
                    )
                ],
            )

            stored = read_jpx_delistings(sqlite_path)

            self.assertEqual([row.ticker for row in stored], ["1000", "2000"])

    def test_storing_zero_rows_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            open_connection(sqlite_path).close()
            with self.assertRaises(CapitalControlError):
                store_jpx_delistings(sqlite_path, [])

    def test_a_header_row_is_not_read_as_a_delisting(self) -> None:
        parser = _TableLinkParser()
        parser.feed(
            "<table><tr><th>上場廃止日</th><th>銘柄名</th><th>コード</th>"
            "<th>市場区分</th><th>上場廃止理由</th></tr>"
            "<tr><td>2026/05/01</td><td>（株）テスト</td><td>2000</td>"
            "<td>プライム</td><td>ＭＢＯ（公開買付け、株式併合）</td></tr></table>"
        )
        rows = list(parser.rows)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][2], "2000")

    def test_a_latin1_decodable_page_is_still_decoded_as_utf8(self) -> None:
        class _Response:
            status_code = 200
            encoding = "ISO-8859-1"
            content = (
                "<table><tr><td>2026/05/01</td><td>（株）テスト</td><td>2000</td>"
                "<td>プライム</td><td>ＭＢＯ（公開買付け、株式併合）</td></tr></table>"
            ).encode()

        class _Session:
            def get(self, url: str, timeout: int) -> _Response:
                return _Response()

        rows = _download_jpx_delistings(_Session())  # type: ignore[arg-type]

        self.assertEqual(rows[0].name, "（株）テスト")
        self.assertEqual(rows[0].reason, "ＭＢＯ（公開買付け、株式併合）")


def _store_documents(sqlite_path: Path, rows: list[tuple[object, ...]]) -> None:
    connection = open_connection(sqlite_path)
    connection.executemany(
        "INSERT INTO edinet_documents("
        "doc_date, sequence_number, doc_id, sec_code, doc_type_code, legal_status, "
        "disclosure_status, withdrawal_status, edinet_code, issuer_edinet_code, "
        "subject_edinet_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    connection.executemany(
        "INSERT OR REPLACE INTO edinet_document_lists("
        "doc_date, process_datetime, result_count, fetched_at_utc, is_final"
        ") VALUES (?, NULL, 1, '2026-08-11T00:00:00+00:00', 1)",
        sorted({(str(row[0]),) for row in rows}),
    )
    connection.commit()
    connection.close()


class ControlEventIndexTest(unittest.TestCase):
    ASOF = date(2026, 8, 10)

    def _listed_window(self, sqlite_path: Path, *, rows: list[tuple[object, ...]]) -> None:
        """Fill every day of the annotation window so coverage is provable."""
        connection = open_connection(sqlite_path)
        cursor = date(2026, 1, 1)
        days = []
        while cursor <= self.ASOF:
            days.append((cursor.isoformat(),))
            cursor = date.fromordinal(cursor.toordinal() + 1)
        connection.executemany(
            "INSERT OR REPLACE INTO edinet_document_lists("
            "doc_date, process_datetime, result_count, fetched_at_utc, is_final"
            ") VALUES (?, NULL, 0, '2026-08-11T00:00:00+00:00', 1)",
            days,
        )
        connection.commit()
        connection.close()
        _store_documents(sqlite_path, rows)

    def test_a_day_that_was_never_listed_makes_the_window_unobserved(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _store_documents(
                sqlite_path,
                [("2026-08-03", 1, "S1", "13010", "350", "1", "0", "0", "E1", "E2", None)],
            )
            connection = connect_current(sqlite_path)
            assert connection is not None
            try:
                covered = edinet_identity_covered(
                    connection, start=date(2026, 8, 1), end=date(2026, 8, 10)
                )
            finally:
                connection.close()

            self.assertFalse(covered)

    def test_a_typed_row_without_an_edinet_code_makes_the_window_unobserved(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            self._listed_window(
                sqlite_path,
                rows=[("2026-08-03", 1, "S1", "13010", "350", "1", "0", "0", None, "E2", None)],
            )
            connection = connect_current(sqlite_path)
            assert connection is not None
            try:
                covered = edinet_identity_covered(connection, start=date(2026, 1, 1), end=self.ASOF)
            finally:
                connection.close()

            self.assertFalse(covered)

    def test_the_index_reads_the_target_company_not_the_filer(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            self._listed_window(
                sqlite_path,
                rows=[
                    # The filer is 9999; the holding is in 1301.
                    ("2026-07-01", 1, "S1", "99990", "350", "1", "0", "0", "E9", "E1", None),
                    # The offeror is 9999; the target is 1332.
                    ("2026-07-02", 1, "S2", None, "240", "1", "0", "0", "E9", None, "E2"),
                    # 1301 and 1332 identify themselves through their own filings.
                    ("2026-06-01", 1, "S3", "13010", "120", "1", "0", "0", "E1", None, None),
                    ("2026-06-02", 1, "S4", "13320", "120", "1", "0", "0", "E2", None, None),
                ],
            )
            connection = connect_current(sqlite_path)
            assert connection is not None
            try:
                index = read_control_event_index(connection, start=date(2026, 1, 1), end=self.ASOF)
            finally:
                connection.close()

            assert index is not None
            self.assertEqual(index.latest_by_target[("1301", "large_holding")], date(2026, 7, 1))
            self.assertEqual(index.latest_by_target[("1332", "tender_offer")], date(2026, 7, 2))
            self.assertNotIn(("9999", "large_holding"), index.latest_by_target)

    def test_a_withdrawn_filing_is_not_an_event(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            self._listed_window(
                sqlite_path,
                rows=[
                    ("2026-07-01", 1, "S1", "99990", "350", "1", "0", "1", "E9", "E1", None),
                    ("2026-06-01", 1, "S3", "13010", "120", "1", "0", "0", "E1", None, None),
                ],
            )
            connection = connect_current(sqlite_path)
            assert connection is not None
            try:
                index = read_control_event_index(connection, start=date(2026, 1, 1), end=self.ASOF)
            finally:
                connection.close()

            assert index is not None
            self.assertEqual(index.latest_by_target, {})

    def test_an_unobserved_window_reports_unknown_rather_than_no_events(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _store_documents(
                sqlite_path,
                [("2026-08-03", 1, "S1", "13010", "120", "1", "0", "0", "E1", None, None)],
            )

            annotations = read_capital_control_annotations(
                sqlite_path, asof=self.ASOF, tickers=["1301"]
            )

            annotation = annotations["1301"]
            self.assertIsNone(annotation.large_holding_event_recent)
            self.assertIsNone(annotation.tender_offer_event_recent)
            self.assertIsNone(annotation.tse_capital_policy_status)

    def test_an_observed_window_without_filings_reports_no_events(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            self._listed_window(
                sqlite_path,
                rows=[("2026-06-01", 1, "S3", "13010", "120", "1", "0", "0", "E1", None, None)],
            )

            annotations = read_capital_control_annotations(
                sqlite_path, asof=self.ASOF, tickers=["1301"]
            )

            annotation = annotations["1301"]
            self.assertIs(annotation.large_holding_event_recent, False)
            self.assertIsNone(annotation.large_holding_event_latest_on)

    def test_a_ticker_whose_edinet_identity_is_unknown_reports_unknown(self) -> None:
        """Coverage of the index does not prove a company could have been looked up."""
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            self._listed_window(
                sqlite_path,
                rows=[
                    # 7777 never filed anything carrying its own securities code, so the
                    # holding filed against it cannot be attributed to a listing.
                    ("2026-07-01", 1, "S1", None, "350", "1", "0", "0", "E9", "E7", None),
                    ("2026-06-01", 1, "S3", "13010", "120", "1", "0", "0", "E1", None, None),
                ],
            )

            annotations = read_capital_control_annotations(
                sqlite_path, asof=self.ASOF, tickers=["1301", "7777"]
            )

            self.assertIs(annotations["1301"].large_holding_event_recent, False)
            self.assertIsNone(annotations["7777"].large_holding_event_recent)

    def test_a_filing_naming_no_target_makes_that_event_type_unknown(self) -> None:
        """An absence is unprovable when a filing could have been about anyone."""
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            self._listed_window(
                sqlite_path,
                rows=[
                    ("2026-07-02", 1, "S2", None, "280", "1", "0", "0", "E9", None, None),
                    ("2026-06-01", 1, "S3", "13010", "120", "1", "0", "0", "E1", None, None),
                ],
            )

            annotation = read_capital_control_annotations(
                sqlite_path, asof=self.ASOF, tickers=["1301"]
            )["1301"]

            self.assertIsNone(annotation.tender_offer_event_recent)
            # The other event type is still answerable from the same window.
            self.assertIs(annotation.large_holding_event_recent, False)

    def test_policy_status_reads_the_latest_snapshot_at_or_before_the_asof(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            self._listed_window(
                sqlite_path,
                rows=[("2026-06-01", 1, "S3", "13010", "120", "1", "0", "0", "E1", None, None)],
            )
            content = _workbook(
                [
                    (
                        "開示企業一覧（2026年6月末時点）",
                        _GROUP_2026,
                        _DETAIL_2026,
                        [_row_2026(1301, "検討中", updated=_naive_datetime(2026, 6, 23))],
                    ),
                    (
                        "【過去分】開示企業一覧（2025年5月末時点）",
                        _GROUP_2025,
                        _DETAIL_2025,
                        [_row_2025(1301, "開示済"), _row_2025(9999, "開示済")],
                    ),
                ]
            )
            store_tse_capital_policy_rows(sqlite_path, parse_tse_capital_policy_workbook(content))

            current = read_capital_control_annotations(
                sqlite_path, asof=self.ASOF, tickers=["1301", "9999"]
            )
            historic = read_capital_control_annotations(
                sqlite_path, asof=date(2025, 12, 31), tickers=["1301"]
            )

            self.assertEqual(current["1301"].tse_capital_policy_status, "considering")
            self.assertEqual(current["1301"].tse_capital_policy_updated_on, date(2026, 6, 23))
            # Absent from the June sheet, so the snapshot answered "not listed".
            self.assertEqual(current["9999"].tse_capital_policy_status, "none")
            self.assertEqual(historic["1301"].tse_capital_policy_status, "disclosed")


if __name__ == "__main__":
    unittest.main()
