from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import cast
from unittest import TestCase

from tests.engine.market_context.indicators.indicator_fixtures import (
    _context_with_browser,
    _FakeBrowserFetcher,
    _FakeResponse,
    _ForbiddenResponse,
    _source,
    _StaticSession,
)

from baibai_engine.data_collection.economic.providers import (
    IndicatorsProviderError,
    parse_fred_csv,
    parse_h15_csv,
)
from baibai_engine.data_collection.economic.providers.base import (
    MAX_CSV_RESPONSE_BYTES,
    BrowserUnavailableError,
    HttpSession,
)
from baibai_engine.data_collection.economic.providers.frb_h15 import FrbH15Source
from baibai_engine.data_collection.economic.providers.umich_sca import parse_umich_table
from baibai_engine.data_collection.economic.sources import require_source
from baibai_engine.machine_analysis.market_context.indicators.definitions import load_definitions


class IndicatorsProviderParserTests(TestCase):
    def test_h15_missing_date_is_not_a_successful_empty_response(self) -> None:
        series = require_source("fed-h15")
        with self.assertRaisesRegex(IndicatorsProviderError, "no Time Period"):
            parse_h15_csv(
                series,
                '"Time Period","RIFLGFCY10_N.B"\n,4.39\n',
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
            )

    def test_parse_fred_csv_filters_range_without_discarding_missing_cells(self) -> None:
        series = _source("fred_csv", "DGS10")
        text = "observation_date,DGS10\n2026-05-01,4.39\n2026-05-02,.\n2026-05-04,4.45\n"

        observations = parse_fred_csv(
            series,
            text,
            start=date(2026, 5, 2),
            end=date(2026, 5, 5),
        )

        self.assertEqual(
            observations,
            {
                date(2026, 5, 2): [{"observation_date": "2026-05-02", "DGS10": "."}],
                date(2026, 5, 4): [{"observation_date": "2026-05-04", "DGS10": "4.45"}],
            },
        )

    def test_parse_fred_csv_rejects_missing_date_header_instead_of_empty_history(self) -> None:
        series = _source("fred_csv", "DGS10")
        with self.assertRaisesRegex(IndicatorsProviderError, "missing column observation_date"):
            parse_fred_csv(
                series,
                "date,DGS10\n2026-05-01,4.39\n",
                start=date(2026, 5, 1),
                end=date(2026, 5, 2),
            )

    def test_parse_umich_table_reads_the_column_for_each_published_month(self) -> None:
        series = require_source("umich-sca-ics")
        # The early history is quarterly, so consecutive rows can skip months.
        text = "Month,YYYY,ICS_ALL\nNovember,1952,86.2\nFebruary,1953,90.7\nJune,2026,49.5\n"

        observations = parse_umich_table(series, text, start=date(1952, 1, 1), end=date(2026, 7, 1))

        self.assertEqual(
            observations,
            {
                date(1952, 11, 1): [{"Month": "November", "YYYY": "1952", "ICS_ALL": "86.2"}],
                date(1953, 2, 1): [{"Month": "February", "YYYY": "1953", "ICS_ALL": "90.7"}],
                date(2026, 6, 1): [{"Month": "June", "YYYY": "2026", "ICS_ALL": "49.5"}],
            },
        )

    def test_parse_umich_table_filters_to_the_requested_window(self) -> None:
        series = require_source("umich-sca-ics")
        text = "Month,YYYY,ICS_ALL\nApril,2026,49.8\nMay,2026,44.8\nJune,2026,49.5\n"

        observations = parse_umich_table(
            series, text, start=date(2026, 5, 1), end=date(2026, 5, 31)
        )

        self.assertEqual(list(observations), [date(2026, 5, 1)])

    def test_parse_umich_table_retains_a_month_published_without_a_value(self) -> None:
        series = require_source("umich-sca-ics")
        text = "Month,YYYY,ICS_ALL\nMay,2026,44.8\nJune,2026,\n"

        observations = parse_umich_table(series, text, start=date(2026, 1, 1), end=date(2026, 7, 1))

        self.assertEqual(
            observations[date(2026, 6, 1)],
            [{"Month": "June", "YYYY": "2026", "ICS_ALL": ""}],
        )

    def test_parse_umich_table_rejects_a_row_that_is_not_a_month_of_a_year(self) -> None:
        series = require_source("umich-sca-ics")
        text = "Month,YYYY,ICS_ALL\nQ2,2026,49.5\n"

        with self.assertRaisesRegex(IndicatorsProviderError, "not a month of a year"):
            parse_umich_table(series, text, start=date(2026, 1, 1), end=date(2026, 7, 1))

    def test_parse_h15_csv_retains_rows_without_the_requested_yield_column(self) -> None:
        series = require_source("fed-h15")
        # Synthetic CSV: acquisition retains a source row even if L2 cannot read
        # this particular registered yield from it after the native save.
        text = '"Time Period",OTHER\n2026-05-01,4.39\n'

        records = parse_h15_csv(series, text, start=date(2026, 5, 1), end=date(2026, 5, 1))

        self.assertEqual(
            records,
            {
                date(2026, 5, 1): [
                    {"row": {"Time Period": "2026-05-01", "OTHER": "4.39"}, "preamble": []}
                ]
            },
        )

    def test_parse_h15_csv_retains_all_columns_missing_markers_and_literal_preamble(self) -> None:
        series = require_source("fed-h15")
        # Deliberately synthetic metadata and cells, not a captured live response.
        preamble = ['"Release","Synthetic H.15 fixture"', "", "  Units: percent  "]
        text = "\n".join(
            [
                *preamble,
                '"Time Period","RIFLGFCY10_N.B","RIFLGFCY02_N.B","RIFLGFCY30_N.B",OTHER',
                '2026-05-01,ND,.,,"source, note"',
                "2026-05-02, 4.390 ,3.880,4.900,",
                "",
            ]
        )

        records = parse_h15_csv(series, text, start=date(2026, 5, 1), end=date(2026, 5, 2))

        self.assertEqual(
            records,
            {
                date(2026, 5, 1): [
                    {
                        "row": {
                            "Time Period": "2026-05-01",
                            "RIFLGFCY10_N.B": "ND",
                            "RIFLGFCY02_N.B": ".",
                            "RIFLGFCY30_N.B": "",
                            "OTHER": "source, note",
                        },
                        "preamble": preamble,
                    }
                ],
                date(2026, 5, 2): [
                    {
                        "row": {
                            "Time Period": "2026-05-02",
                            "RIFLGFCY10_N.B": " 4.390 ",
                            "RIFLGFCY02_N.B": "3.880",
                            "RIFLGFCY30_N.B": "4.900",
                            "OTHER": "",
                        },
                        "preamble": preamble,
                    }
                ],
            },
        )

    def test_parse_h15_csv_retains_duplicate_rows_in_source_order(self) -> None:
        series = require_source("fed-h15")
        text = (
            '"Time Period","RIFLGFCY10_N.B",OTHER\n'
            "2026-05-01,4.39,first\n"
            "2026-05-01,4.39,first\n"
            "2026-05-01,ND,revision\n"
        )

        records = parse_h15_csv(series, text, start=date(2026, 5, 1), end=date(2026, 5, 1))

        self.assertEqual(
            records,
            {
                date(2026, 5, 1): [
                    {
                        "row": {
                            "Time Period": "2026-05-01",
                            "RIFLGFCY10_N.B": "4.39",
                            "OTHER": "first",
                        },
                        "preamble": [],
                    },
                    {
                        "row": {
                            "Time Period": "2026-05-01",
                            "RIFLGFCY10_N.B": "4.39",
                            "OTHER": "first",
                        },
                        "preamble": [],
                    },
                    {
                        "row": {
                            "Time Period": "2026-05-01",
                            "RIFLGFCY10_N.B": "ND",
                            "OTHER": "revision",
                        },
                        "preamble": [],
                    },
                ]
            },
        )

    def test_parse_h15_csv_filters_dates_without_discarding_native_missing_values(self) -> None:
        series = require_source("fed-h15")
        text = (
            '"Time Period","RIFLGFCY10_N.B"\n'
            "2026-04-30,4.38\n"
            "2026-05-01,ND\n"
            "2026-05-02,\n"
            "2026-05-03,4.40\n"
        )

        records = parse_h15_csv(series, text, start=date(2026, 5, 1), end=date(2026, 5, 2))

        self.assertEqual(
            records,
            {
                date(2026, 5, 1): [
                    {
                        "row": {"Time Period": "2026-05-01", "RIFLGFCY10_N.B": "ND"},
                        "preamble": [],
                    }
                ],
                date(2026, 5, 2): [
                    {
                        "row": {"Time Period": "2026-05-02", "RIFLGFCY10_N.B": ""},
                        "preamble": [],
                    }
                ],
            },
        )

    def test_parse_h15_csv_rejects_duplicate_columns_and_ragged_rows(self) -> None:
        series = require_source("fed-h15")
        for text in (
            '"Time Period","RIFLGFCY10_N.B","RIFLGFCY10_N.B"\n2026-05-01,4.39,4.40\n',
            '"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39,extra\n',
            '"Time Period","RIFLGFCY10_N.B"\n2026-05-01\n',
        ):
            with self.subTest(text=text), self.assertRaises(IndicatorsProviderError):
                parse_h15_csv(series, text, start=date(2026, 5, 1), end=date(2026, 5, 1))

    def test_parse_h15_csv_preserves_newlines_inside_quoted_native_cells(self) -> None:
        series = require_source("fed-h15")
        text = '"Time Period","RIFLGFCY10_N.B",OTHER\n2026-05-01,4.39,"first\nsecond"\n'
        records = parse_h15_csv(series, text, start=date(2026, 5, 1), end=date(2026, 5, 1))
        self.assertEqual(records[date(2026, 5, 1)][0]["row"]["OTHER"], "first\nsecond")

    def test_parse_h15_csv_reports_non_csv_response_with_snippet(self) -> None:
        series = require_source("fed-h15")
        text = "<!DOCTYPE html>\n<html><head><title>Access Denied</title></head></html>"

        with self.assertRaisesRegex(
            IndicatorsProviderError,
            r"missing Time Period header; response starts with: '<!DOCTYPE html> <html>",
        ):
            parse_h15_csv(series, text, start=date(2026, 5, 1), end=date(2026, 5, 1))

    def test_h15_fetch_sends_browser_user_agent(self) -> None:
        series = require_source("fed-h15")
        captured: dict[str, object] = {}

        class _Response:
            status_code = 200
            headers: dict[str, str] = {}

            def raise_for_status(self) -> None:
                return None

            def iter_content(self, chunk_size: int) -> object:
                del chunk_size
                return iter([b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n'])

            def close(self) -> None:
                return None

        class _Session:
            def get(self, url: str, **kwargs: object) -> _Response:
                captured["url"] = url
                captured["headers"] = kwargs.get("headers")
                return _Response()

        records = FrbH15Source().fetch_records(
            series,
            start=date(2026, 5, 1),
            end=date(2026, 5, 1),
            session=cast(HttpSession, _Session()),
        )

        self.assertEqual(
            records,
            {
                date(2026, 5, 1): [
                    {
                        "row": {"Time Period": "2026-05-01", "RIFLGFCY10_N.B": "4.39"},
                        "preamble": [],
                    }
                ]
            },
        )
        headers = cast("dict[str, str]", captured["headers"])
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        # Companion browser headers reduce the datacenter-IP block-page rate.
        self.assertIn("Accept", headers)
        self.assertIn("Accept-Language", headers)

    def test_h15_falls_back_to_a_browser_when_the_plain_response_is_empty(self) -> None:
        series = require_source("fed-h15")
        browser = _FakeBrowserFetcher(b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n')

        with _context_with_browser(browser) as context:
            records = FrbH15Source().fetch_records(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(b""))),
                context=context,
            )

        self.assertEqual(
            records,
            {
                date(2026, 5, 1): [
                    {
                        "row": {"Time Period": "2026-05-01", "RIFLGFCY10_N.B": "4.39"},
                        "preamble": [],
                    }
                ]
            },
        )
        self.assertEqual(len(browser.urls), 1)
        # The browser navigates to the same query the plain client sent.
        self.assertIn("series=bf17364827e38702b42a58cf8eaa3f78", browser.urls[0])
        self.assertIn("from=05%2F01%2F2026", browser.urls[0])

    def test_h15_falls_back_to_a_browser_when_the_plain_response_is_forbidden(self) -> None:
        series = require_source("fed-h15")
        browser = _FakeBrowserFetcher(b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n')

        with _context_with_browser(browser) as context:
            records = FrbH15Source().fetch_records(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                # A Cloudflare bot-mitigation challenge arrives as a 403 as
                # readily as it arrives as an empty body.
                session=cast(HttpSession, _StaticSession(_ForbiddenResponse())),
                context=context,
            )

        self.assertEqual(
            records,
            {
                date(2026, 5, 1): [
                    {
                        "row": {"Time Period": "2026-05-01", "RIFLGFCY10_N.B": "4.39"},
                        "preamble": [],
                    }
                ]
            },
        )
        self.assertEqual(len(browser.urls), 1)

    def test_h15_falls_back_to_a_browser_when_the_plain_response_is_a_block_page(self) -> None:
        series = require_source("fed-h15")
        browser = _FakeBrowserFetcher(b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n')
        block_page = b"<!DOCTYPE html><html><title>Access Denied</title></html>"

        with _context_with_browser(browser) as context:
            records = FrbH15Source().fetch_records(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(block_page))),
                context=context,
            )

        self.assertEqual(
            records,
            {
                date(2026, 5, 1): [
                    {
                        "row": {"Time Period": "2026-05-01", "RIFLGFCY10_N.B": "4.39"},
                        "preamble": [],
                    }
                ]
            },
        )
        self.assertEqual(len(browser.urls), 1)

    def test_h15_browser_fallback_downloads_once_for_every_series_in_a_pass(self) -> None:
        # Synthetic package includes all three registered Treasury yield columns.
        source = require_source("fed-h15")
        readings = load_definitions().by_id()
        assert source.provider_series_id == ""
        assert {readings[key].source_id for key in ("us.10y", "us.2y", "us.30y")} == {
            source.source_id
        }
        assert len({readings[key].reading_key for key in ("us.10y", "us.2y", "us.30y")}) == 3
        browser = _FakeBrowserFetcher(
            b'"Time Period","RIFLGFCY10_N.B","RIFLGFCY02_N.B","RIFLGFCY30_N.B"\n'
            b"2026-05-01,4.39,3.88,4.90\n"
        )
        session = cast(HttpSession, _StaticSession(_FakeResponse(b"")))
        expected = {
            date(2026, 5, 1): [
                {
                    "row": {
                        "Time Period": "2026-05-01",
                        "RIFLGFCY10_N.B": "4.39",
                        "RIFLGFCY02_N.B": "3.88",
                        "RIFLGFCY30_N.B": "4.90",
                    },
                    "preamble": [],
                }
            ]
        }

        with _context_with_browser(browser) as context:
            for provider_series_id in ("RIFLGFCY10_N.B", "RIFLGFCY02_N.B", "RIFLGFCY30_N.B"):
                with self.subTest(provider_series_id=provider_series_id):
                    records = FrbH15Source().fetch_records(
                        require_source("fed-h15"),
                        start=date(2026, 5, 1),
                        end=date(2026, 5, 1),
                        session=session,
                        context=context,
                    )
                    self.assertEqual(records, expected)

        # All three series retain the whole package from one cached download,
        # rather than paying for another browser navigation per requested column.
        self.assertEqual(len(browser.urls), 1)

    def test_h15_plain_download_is_shared_for_all_registered_treasury_columns(self) -> None:
        body = (
            b'"Time Period","RIFLGFCY10_N.B","RIFLGFCY02_N.B","RIFLGFCY30_N.B"\n2026-05-01,ND,.,\n'
        )
        requests: list[str] = []

        class _Session:
            def get(self, url: str, **kwargs: object) -> _FakeResponse:
                del kwargs
                requests.append(url)
                return _FakeResponse(body)

        browser = _FakeBrowserFetcher(AssertionError("plain CSV should not need a browser"))
        expected = {
            date(2026, 5, 1): [
                {
                    "row": {
                        "Time Period": "2026-05-01",
                        "RIFLGFCY10_N.B": "ND",
                        "RIFLGFCY02_N.B": ".",
                        "RIFLGFCY30_N.B": "",
                    },
                    "preamble": [],
                }
            ]
        }

        with _context_with_browser(browser) as context:
            for provider_series_id in ("RIFLGFCY10_N.B", "RIFLGFCY02_N.B", "RIFLGFCY30_N.B"):
                with self.subTest(provider_series_id=provider_series_id):
                    records = FrbH15Source().fetch_records(
                        require_source("fed-h15"),
                        start=date(2026, 5, 1),
                        end=date(2026, 5, 1),
                        session=cast(HttpSession, _Session()),
                        context=context,
                    )
                    self.assertEqual(records, expected)

        self.assertEqual(len(requests), 1)
        self.assertEqual(browser.urls, [])

    def test_h15_reports_the_plain_failure_when_the_browser_is_also_blocked(self) -> None:
        series = require_source("fed-h15")
        browser = _FakeBrowserFetcher(IndicatorsProviderError("browser could not download"))

        with (
            _context_with_browser(browser) as context,
            self.assertRaisesRegex(
                IndicatorsProviderError,
                r"browser could not download; the plain client first failed with: "
                r"empty response body from .* \(status 200, content-type 'text/html'\)",
            ),
        ):
            FrbH15Source().fetch_records(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(
                    HttpSession,
                    _StaticSession(_FakeResponse(b"", headers={"Content-Type": "text/html"})),
                ),
                context=context,
            )

    def test_h15_reports_the_block_page_when_the_browser_is_also_blocked(self) -> None:
        series = require_source("fed-h15")
        browser = _FakeBrowserFetcher(IndicatorsProviderError("browser could not download"))
        block_page = b"<!DOCTYPE html><html><title>Access Denied</title></html>"

        with (
            _context_with_browser(browser) as context,
            # A block page arrives as a normal 2xx, so the evidence of how the
            # edge blocked has to survive the fallback just like an empty body.
            self.assertRaisesRegex(
                IndicatorsProviderError,
                r"the plain client first failed with: response without the CSV header: "
                r"'<!DOCTYPE html><html><title>Access Denied",
            ),
        ):
            FrbH15Source().fetch_records(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(block_page))),
                context=context,
            )

    def test_h15_rejects_a_browser_response_without_the_csv_header(self) -> None:
        series = require_source("fed-h15")
        browser = _FakeBrowserFetcher(b"<!DOCTYPE html><html><title>Just a moment</title></html>")

        with _context_with_browser(browser) as context:
            with self.assertRaisesRegex(
                IndicatorsProviderError, r"browser response without the CSV header"
            ):
                FrbH15Source().fetch_records(
                    series,
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 1),
                    session=cast(HttpSession, _StaticSession(_FakeResponse(b""))),
                    context=context,
                )

            # An unvalidated browser answer in the cache would fail every later
            # series sharing the URL with the parser's format error.
            self.assertEqual(context.bytes_cache, {})

    def test_h15_rejects_a_browser_response_that_is_not_utf8(self) -> None:
        series = require_source("fed-h15")
        browser = _FakeBrowserFetcher(b"\xff\xfe\x00garbage")

        with _context_with_browser(browser) as context:
            # A decode error is not an IndicatorsProviderError, so escaping as one
            # would deny the refresh its retry and leave the bytes cached.
            with self.assertRaises(IndicatorsProviderError):
                FrbH15Source().fetch_records(
                    series,
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 1),
                    session=cast(HttpSession, _StaticSession(_FakeResponse(b""))),
                    context=context,
                )

            self.assertEqual(context.bytes_cache, {})

    def test_h15_does_not_re_navigate_after_the_browser_was_blocked_in_this_pass(self) -> None:
        browser = _FakeBrowserFetcher(IndicatorsProviderError("navigation timed out"))
        session = cast(HttpSession, _StaticSession(_FakeResponse(b"")))
        reported: list[str] = []

        with _context_with_browser(browser) as context:
            for provider_series_id in ("RIFLGFCY10_N.B", "RIFLGFCY02_N.B"):
                with (
                    self.subTest(reading_key=provider_series_id),
                    self.assertRaises(IndicatorsProviderError) as caught,
                ):
                    FrbH15Source().fetch_records(
                        require_source("fed-h15"),
                        start=date(2026, 5, 1),
                        end=date(2026, 5, 1),
                        session=session,
                        context=context,
                    )
                reported.append(str(caught.exception))

        # Each navigation costs a timeout, so a blocked edge must be paid for once
        # per pass rather than once per series and attempt.
        self.assertEqual(len(browser.urls), 1)
        # The series that skipped the navigation is the only one still reporting,
        # so what the edge answered has to travel with the record of the block.
        for message in reported:
            self.assertIn("navigation timed out", message)

    def test_h15_retries_the_browser_after_it_could_not_be_started(self) -> None:
        browser = _FakeBrowserFetcher(BrowserUnavailableError("failed to launch"))
        session = cast(HttpSession, _StaticSession(_FakeResponse(b"")))
        reached_browser = 0

        with _context_with_browser(browser) as context:
            for provider_series_id in ("RIFLGFCY10_N.B", "RIFLGFCY02_N.B"):
                with (
                    self.subTest(reading_key=provider_series_id),
                    self.assertRaises(IndicatorsProviderError),
                ):
                    FrbH15Source().fetch_records(
                        require_source("fed-h15"),
                        start=date(2026, 5, 1),
                        end=date(2026, 5, 1),
                        session=session,
                        context=context,
                    )
                reached_browser = len(browser.urls)

            # A browser that never started says nothing about the source, so the
            # next series has the same reason to try as this one did.
            self.assertEqual(context.blocked_browser_urls, {})

        self.assertEqual(reached_browser, 2)

    def test_h15_does_not_fall_back_to_a_browser_for_a_failure_that_is_not_a_block(self) -> None:
        series = require_source("fed-h15")
        browser = _FakeBrowserFetcher(b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n')
        oversized = _FakeResponse(b"x" * 32, headers={"Content-Length": "9000000"})

        with _context_with_browser(browser) as context:
            with self.assertRaisesRegex(IndicatorsProviderError, r"response too large"):
                FrbH15Source().fetch_records(
                    series,
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 1),
                    session=cast(HttpSession, _StaticSession(oversized)),
                    context=context,
                )

            # The size cap did its job; re-fetching the same resource through a
            # browser would have it written to disk with no cap in front of it.
            self.assertEqual(browser.urls, [])

    def test_h15_browser_navigation_preserves_a_query_already_in_the_source_url(self) -> None:
        series = require_source("fed-h15")
        series = replace(series, source_url="https://example.test/Output.aspx?rel=H15")
        browser = _FakeBrowserFetcher(b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n')

        with _context_with_browser(browser) as context:
            FrbH15Source().fetch_records(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(b""))),
                context=context,
            )

        # The plain client merges params into an existing query; the navigation
        # must address the same resource rather than a doubled "?".
        self.assertEqual(browser.urls[0].count("?"), 1)
        self.assertIn("rel=H15", browser.urls[0])
        self.assertIn("layout=seriescolumn", browser.urls[0])

    def test_h15_browser_download_is_capped_by_the_provider_limit(self) -> None:
        series = require_source("fed-h15")
        browser = _FakeBrowserFetcher(b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n')

        with _context_with_browser(browser) as context:
            FrbH15Source().fetch_records(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(b""))),
                context=context,
            )

        # One resource, one ceiling — not a second one that depends on the route.
        self.assertEqual(browser.max_bytes, [MAX_CSV_RESPONSE_BYTES])

    def test_h15_without_a_context_reports_the_response_instead_of_falling_back(self) -> None:
        series = require_source("fed-h15")
        block_page = b"<!DOCTYPE html><html><title>Access Denied</title></html>"

        with self.assertRaisesRegex(
            IndicatorsProviderError, r"missing Time Period header; response starts with: '<!DOCTYPE"
        ):
            FrbH15Source().fetch_records(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(block_page))),
            )
