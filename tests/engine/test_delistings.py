from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from tests.helpers.http_doubles import always, html_response

from baibai_engine.market.delistings import (
    DelistingRecord,
    DelistingSourceError,
    download_jpx_delistings,
    read_jpx_delistings,
    store_jpx_delistings,
)
from baibai_engine.storage.observations.schema import open_connection


class JPXDelistingTest(unittest.TestCase):
    def test_delistings_accumulate_across_refreshes(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            open_connection(sqlite_path).close()
            store_jpx_delistings(
                sqlite_path,
                [
                    DelistingRecord(
                        delisted_on=date(2024, 5, 1),
                        ticker="1000",
                        name="旧年の会社",
                        market="スタンダード",
                        reason="株式の併合",
                    )
                ],
            )
            store_jpx_delistings(
                sqlite_path,
                [
                    DelistingRecord(
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
            with self.assertRaises(DelistingSourceError):
                store_jpx_delistings(sqlite_path, [])

    def test_a_header_row_is_not_read_as_a_delisting(self) -> None:
        session = always(html_response(
            "<table><tr><th>上場廃止日</th><th>銘柄名</th><th>コード</th>"
            "<th>市場区分</th><th>上場廃止理由</th></tr>"
            "<tr><td>2026/05/01</td><td>（株）テスト</td><td>2000</td>"
            "<td>プライム</td><td>ＭＢＯ（公開買付け、株式併合）</td></tr></table>".encode()
        ))
        records = download_jpx_delistings(session)  # type: ignore[arg-type]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].ticker, "2000")
        self.assertEqual(records[0].delisted_on, date(2026, 5, 1))


    def test_only_jpx_archive_links_are_followed(self) -> None:
        index_html = (
            "<table><tr><td>2026/05/01</td><td>（株）テスト</td><td>2000</td>"
            "<td>プライム</td><td>ＭＢＯ（公開買付け、株式併合）</td></tr></table>"
            '<a href="archives-01.html">過去分</a>'
            '<a href="https://evil.example/listing/stocks/delisted/archives-02.html">x</a>'
            '<a href="https://www.jpx.co.jp.evil.example/listing/stocks/delisted/'
            'archives-03.html">x</a>'
            '<a href="//evil.example/listing/stocks/delisted/archives-04.html">x</a>'
            '<a href="http://www.jpx.co.jp/listing/stocks/delisted/archives-05.html">x</a>'
            '<a href="https://www.jpx.co.jp/listing/stocks/delisted/'
            'archives-06.html?to=evil">x</a>'
            '<a href="https://www.jpx.co.jp/markets/statistics-equities/index.html">x</a>'
            '<option value="https://www.jpx.co.jp/listing/stocks/delisted/'
            'archives-07.html">x</option>'
        )
        archive_html = (
            "<table><tr><td>2024/05/01</td><td>（株）旧</td><td>1000</td>"
            "<td>スタンダード</td><td>株式の併合</td></tr></table>"
        )

        class _Response:
            def __init__(self, body: str) -> None:
                self.status_code = 200
                self.encoding = "ISO-8859-1"
                self.content = body.encode()

        class _Session:
            def __init__(self) -> None:
                self.requested: list[str] = []

            def get(self, url: str, timeout: int) -> _Response:
                self.requested.append(url)
                return _Response(index_html if url.endswith("delisted/") else archive_html)

        session = _Session()
        rows = download_jpx_delistings(session)  # type: ignore[arg-type]

        self.assertEqual(
            sorted(session.requested),
            [
                "https://www.jpx.co.jp/listing/stocks/delisted/",
                "https://www.jpx.co.jp/listing/stocks/delisted/archives-01.html",
                "https://www.jpx.co.jp/listing/stocks/delisted/archives-07.html",
            ],
        )
        self.assertEqual(sorted(row.ticker for row in rows), ["1000", "2000"])

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

        rows = download_jpx_delistings(_Session())  # type: ignore[arg-type]

        self.assertEqual(rows[0].name, "（株）テスト")
        self.assertEqual(rows[0].reason, "ＭＢＯ（公開買付け、株式併合）")


class DelistingSessionOwnershipTests(unittest.TestCase):
    def test_owned_session_is_released_on_success_and_failure(self) -> None:
        for status in (200, 503):
            with self.subTest(status=status):
                session = MagicMock()
                session.__enter__.return_value = session
                session.get.return_value.status_code = status
                session.get.return_value.content = (
                    b"<table><tr><td>2026/05/01</td><td>Test</td><td>2000</td>"
                    b"<td>Prime</td><td>Merger</td></tr></table>"
                )
                with patch(
                    "baibai_engine.market.delistings.requests.Session", return_value=session
                ):
                    if status == 200:
                        self.assertEqual(download_jpx_delistings()[0].ticker, "2000")
                    else:
                        with self.assertRaises(DelistingSourceError):
                            download_jpx_delistings()
                session.__exit__.assert_called_once()

    def test_borrowed_session_is_not_closed_on_failure(self) -> None:
        session = MagicMock()
        session.get.return_value.status_code = 503
        with self.assertRaises(DelistingSourceError):
            download_jpx_delistings(session)
        session.close.assert_not_called()
        session.__exit__.assert_not_called()
