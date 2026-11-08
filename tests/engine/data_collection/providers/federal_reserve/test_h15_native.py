"""Native source contracts for h15_native."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import cast
from unittest import TestCase
from unittest.mock import patch

import requests
from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.provider_fixtures import (
    _context_with_browser,
    _FakeBrowserFetcher,
    _FakeResponse,
    _ForbiddenResponse,
    _StaticSession,
)

from baibai_engine.data_collection.catalog import require_source
from baibai_engine.data_collection.providers.federal_reserve.h15 import FrbH15Source, parse_h15_csv
from baibai_engine.data_collection.transport.context import (
    MAX_CSV_RESPONSE_BYTES,
    BrowserUnavailableError,
    HttpSession,
    IndicatorsProviderError,
    ProviderFetchStoppedError,
    SourceWithheldError,
)
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
    load_definitions,
)


class NativeParserTests(TestCase):
    def test_h15_missing_date_is_not_a_successful_empty_response(self) -> None:
        series = require_source("fed-h15")
        with self.assertRaisesRegex(IndicatorsProviderError, "no Time Period"):
            parse_h15_csv(
                series,
                '"Time Period","RIFLGFCY10_N.B"\n,4.39\n',
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
            )

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
        # Preserve the configured request headers; this fixture proves no live access cause.
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

    def test_h15_keeps_a_forbidden_response_without_a_browser_route(self) -> None:
        series = require_source("fed-h15")
        browser = _FakeBrowserFetcher(b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n')

        with _context_with_browser(browser) as context:
            with (
                patch.object(context, "discard_cached_bytes") as discard,
                self.assertRaises(SourceWithheldError) as caught,
            ):
                FrbH15Source().fetch_records(
                    series,
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 1),
                    session=cast(HttpSession, _StaticSession(_ForbiddenResponse())),
                    context=context,
                )
            discard.assert_not_called()
            self.assertEqual(context.bytes_cache, {})

        self.assertEqual(caught.exception.status_code, 403)
        self.assertIsInstance(caught.exception.__cause__, requests.HTTPError)
        self.assertEqual(str(caught.exception.__cause__), "403 Client Error: Forbidden")
        self.assertEqual(browser.urls, [])

    def test_h15_http_and_decode_failures_do_not_select_a_browser_route(self) -> None:
        class _HttpErrorResponse(_FakeResponse):
            def raise_for_status(self) -> None:
                response = requests.Response()
                response.status_code = self.status_code
                raise requests.HTTPError(f"HTTP {self.status_code}", response=response)

        responses = [
            *(_HttpErrorResponse(b"fixture", status_code=status) for status in (404, 429, 503)),
            _FakeResponse(b"\xff"),
        ]
        for response in responses:
            browser = _FakeBrowserFetcher(AssertionError("failure must not select a browser"))
            with (
                self.subTest(status=response.status_code, body=response.content),
                _context_with_browser(browser) as context,
                patch("baibai_engine.data_collection.transport.context.wait_for_read_retry"),
                patch.object(
                    context, "discard_cached_bytes", wraps=context.discard_cached_bytes
                ) as discard,
                self.assertRaises(IndicatorsProviderError),
            ):
                FrbH15Source().fetch_records(
                    require_source("fed-h15"),
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 1),
                    session=cast(HttpSession, _StaticSession(response)),
                    context=context,
                )
            if response.content == b"\xff":
                # The text transport evicts undecodable bytes without another GET.
                self.assertEqual(discard.call_count, 1)
            else:
                discard.assert_not_called()
            self.assertEqual(context.bytes_cache, {})
            self.assertEqual(browser.urls, [])

    def test_h15_falls_back_to_a_browser_when_the_plain_success_needs_rendering(self) -> None:
        series = require_source("fed-h15")
        browser = _FakeBrowserFetcher(b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n')
        render_page = (
            b"<!DOCTYPE html><html><title>H.15 Data Download</title>"
            b"<!-- <title>Access Denied</title> --><body>Access Denied is not the title</body></html>"
        )

        with _context_with_browser(browser) as context:
            records = FrbH15Source().fetch_records(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(render_page))),
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

    def test_h15_explicit_denial_in_a_successful_response_never_selects_another_route(self) -> None:
        for body in (
            b"<!DOCTYPE html><html><title>Access Denied</title></html>",
            b"<HTML><TITLE> \n ACCESS\tDENIED </TITLE></HTML>",
            b"<title>Access&#32;Denied</title>",
        ):
            with (
                self.subTest(body=body),
                _context_with_browser(
                    _FakeBrowserFetcher(AssertionError("explicit denial must not use a browser"))
                ) as context,
            ):
                response = _FakeResponse(body)
                session = _StaticSession(response)
                with (
                    patch.object(session, "get", wraps=session.get) as fetch,
                    patch.object(context, "discard_cached_bytes") as discard,
                    patch.object(context, "browser_fetcher") as browser,
                    self.assertRaisesRegex(
                        ProviderFetchStoppedError, "successful response explicitly denies access"
                    ) as caught,
                ):
                    FrbH15Source().fetch_records(
                        require_source("fed-h15"),
                        start=date(2026, 5, 1),
                        end=date(2026, 5, 1),
                        session=cast(HttpSession, session),
                        context=context,
                    )
                discard.assert_not_called()
                browser.assert_not_called()
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(list(context.bytes_cache.values()), [body])
                self.assertNotIsInstance(caught.exception, SourceWithheldError)
                self.assertNotIn("403", str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)

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

    def test_h15_reports_the_render_page_when_the_browser_is_also_blocked(self) -> None:
        series = require_source("fed-h15")
        browser = _FakeBrowserFetcher(IndicatorsProviderError("browser could not download"))
        render_page = b"<!DOCTYPE html><html><title>H.15 Data Download</title></html>"

        with (
            _context_with_browser(browser) as context,
            self.assertRaisesRegex(
                IndicatorsProviderError,
                r"the plain client first failed with: response without the CSV header: "
                r"'<!DOCTYPE html><html><title>H.15 Data Download",
            ),
        ):
            FrbH15Source().fetch_records(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(render_page))),
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
            # Bad browser bytes remain a provider failure and never enter the cache.
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

    def test_h15_deadline_is_not_remembered_as_a_source_refusal(self) -> None:
        browser = _FakeBrowserFetcher(AssertionError("deadline must prevent navigation"))
        with _context_with_browser(browser) as context:
            with (
                patch.object(
                    context,
                    "browser_fetcher",
                    side_effect=ProviderFetchStoppedError("acquisition job deadline exhausted"),
                ),
                self.assertRaisesRegex(ProviderFetchStoppedError, "deadline exhausted"),
            ):
                FrbH15Source().fetch_records(
                    require_source("fed-h15"),
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 1),
                    session=cast(HttpSession, _StaticSession(_FakeResponse(b""))),
                    context=context,
                )
            self.assertEqual(context.blocked_browser_urls, {})
            self.assertEqual(browser.urls, [])

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

            # A known size-limit failure is not a reason to fetch the same
            # resource again through another transport.
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
        render_page = b"<!DOCTYPE html><html><title>H.15 Data Download</title></html>"

        with self.assertRaisesRegex(
            IndicatorsProviderError, r"missing Time Period header; response starts with: '<!DOCTYPE"
        ):
            FrbH15Source().fetch_records(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(render_page))),
            )

    def test_h15_explicit_denial_without_a_context_still_reports_the_successful_body(self) -> None:
        body = b"<!DOCTYPE html><html><title>Access Denied</title></html>"
        with self.assertRaisesRegex(
            ProviderFetchStoppedError,
            "successful response explicitly denies access: '<!DOCTYPE html>",
        ):
            FrbH15Source().fetch_records(
                require_source("fed-h15"),
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(body))),
            )
