"""Native source contracts for h15_native."""

from __future__ import annotations

from datetime import date
from typing import cast
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import urlencode

import requests
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
    FetchContext,
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
        with self.assertRaisesRegex(IndicatorsProviderError, "no Time Period"):
            parse_h15_csv(
                '"Time Period","RIFLGFCY10_N.B"\n,4.39\n',
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
            )

    def test_parse_h15_csv_retains_rows_without_the_requested_yield_column(self) -> None:
        # Synthetic CSV: acquisition retains a source row even if L2 cannot read
        # this particular registered yield from it after the native save.
        text = '"Time Period",OTHER\n2026-05-01,4.39\n'

        records = parse_h15_csv(text, start=date(2026, 5, 1), end=date(2026, 5, 1))

        self.assertEqual(
            records,
            {
                date(2026, 5, 1): [
                    {"row": {"Time Period": "2026-05-01", "OTHER": "4.39"}, "preamble": []}
                ]
            },
        )

    def test_parse_h15_csv_retains_all_columns_missing_markers_and_literal_preamble(self) -> None:
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

        records = parse_h15_csv(text, start=date(2026, 5, 1), end=date(2026, 5, 2))

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
        text = (
            '"Time Period","RIFLGFCY10_N.B",OTHER\n'
            "2026-05-01,4.39,first\n"
            "2026-05-01,4.39,first\n"
            "2026-05-01,ND,revision\n"
        )

        records = parse_h15_csv(text, start=date(2026, 5, 1), end=date(2026, 5, 1))

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
        text = (
            '"Time Period","RIFLGFCY10_N.B"\n'
            "2026-04-30,4.38\n"
            "2026-05-01,ND\n"
            "2026-05-02,\n"
            "2026-05-03,4.40\n"
        )

        records = parse_h15_csv(text, start=date(2026, 5, 1), end=date(2026, 5, 2))

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
        for text in (
            '"Time Period","RIFLGFCY10_N.B","RIFLGFCY10_N.B"\n2026-05-01,4.39,4.40\n',
            '"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39,extra\n',
            '"Time Period","RIFLGFCY10_N.B"\n2026-05-01\n',
        ):
            with self.subTest(text=text), self.assertRaises(IndicatorsProviderError):
                parse_h15_csv(text, start=date(2026, 5, 1), end=date(2026, 5, 1))

    def test_parse_h15_csv_preserves_newlines_inside_quoted_native_cells(self) -> None:
        text = '"Time Period","RIFLGFCY10_N.B",OTHER\n2026-05-01,4.39,"first\nsecond"\n'
        records = parse_h15_csv(text, start=date(2026, 5, 1), end=date(2026, 5, 1))
        self.assertEqual(records[date(2026, 5, 1)][0]["row"]["OTHER"], "first\nsecond")

    def test_parse_h15_csv_reports_non_csv_response_with_snippet(self) -> None:
        text = "<!DOCTYPE html>\n<html><head><title>Download unavailable</title></head></html>"

        with self.assertRaisesRegex(
            IndicatorsProviderError,
            r"missing Time Period header; response starts with: '<!DOCTYPE html> <html>",
        ):
            parse_h15_csv(text, start=date(2026, 5, 1), end=date(2026, 5, 1))

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
                patch(
                    "baibai_engine.data_collection.transport.context.wait_for_read_retry"
                ) as retry,
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
            retry.assert_not_called()
            if response.content == b"\xff":
                # The text transport evicts undecodable bytes without another GET.
                self.assertEqual(discard.call_count, 1)
            else:
                discard.assert_not_called()
            self.assertEqual(context.bytes_cache, {})
            self.assertEqual(browser.urls, [])

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

    def test_one_published_package_serves_disjoint_windows_without_retaining_other_dates(
        self,
    ) -> None:
        source = require_source("fed-h15")
        readings = load_definitions().by_id()
        assert {readings[key].source_id for key in ("us.10y", "us.2y", "us.30y")} == {
            source.source_id
        }
        preamble = '"Release","Synthetic metadata with literal spaces  "'
        header = '"Time Period",RIFLGFCY02_N.B,RIFLGFCY10_N.B,RIFLGFCY30_N.B,OTHER'
        text = "\n".join(
            (
                preamble,
                header,
                "2026-05-01,ND,.,,first",
                "2026-05-04,2.1,3.2,4.3,second",
                "2026-05-05,2.2,3.3,4.4,outside",
            )
        )
        session = _StaticSession(_FakeResponse(text.encode()))
        with FetchContext() as context, patch.object(session, "get", wraps=session.get) as fetch:
            for day, expected in (
                (date(2026, 5, 1), ("ND", ".", "", "first")),
                (date(2026, 5, 4), ("2.1", "3.2", "4.3", "second")),
            ):
                records = FrbH15Source().fetch_records(
                    source, start=day, end=day, session=cast(HttpSession, session), context=context
                )
                self.assertEqual(
                    records,
                    {
                        day: [
                            {
                                "row": dict(
                                    zip(
                                        (
                                            "Time Period",
                                            "RIFLGFCY02_N.B",
                                            "RIFLGFCY10_N.B",
                                            "RIFLGFCY30_N.B",
                                            "OTHER",
                                        ),
                                        (day.isoformat(), *expected),
                                        strict=True,
                                    )
                                ),
                                "preamble": [preamble],
                            }
                        ]
                    },
                )
        fetch.assert_called_once()
        assert fetch.call_args is not None
        self.assertIs(fetch.call_args.kwargs["allow_redirects"], False)
        url = fetch.call_args.args[0] + "?" + urlencode(fetch.call_args.kwargs["params"])
        self.assertEqual(
            url,
            "https://www.federalreserve.gov/datadownload/Output.aspx?"
            "filetype=csv&from=&label=include&lastobs=&layout=seriescolumn&rel=H15&"
            "series=bf17364827e38702b42a58cf8eaa3f78&to=&type=package",
        )

    def test_automated_package_failure_is_one_get_without_browser_or_another_url(self) -> None:
        for response, reason in (
            (_FakeResponse(b""), "empty response"),
            (_FakeResponse(b"<title>H.15 Download</title>"), "missing Time Period header"),
            (_FakeResponse(b"Time Period,OTHER\n"), "full package contains no observations"),
            (
                _FakeResponse(
                    b"Time Period,OTHER\n2026-05-01,wrong",
                    status_code=302,
                    headers={"Location": "https://example.test/other"},
                ),
                "not following redirect",
            ),
            (
                _FakeResponse(
                    b"wrong", headers={"Content-Length": str(MAX_CSV_RESPONSE_BYTES + 1)}
                ),
                "too large",
            ),
        ):
            session = _StaticSession(response)
            with (
                self.subTest(reason=reason),
                FetchContext() as context,
                patch.object(session, "get", wraps=session.get) as fetch,
                patch.object(context, "browser_fetcher") as browser,
                self.assertRaisesRegex(IndicatorsProviderError, reason),
            ):
                FrbH15Source().fetch_records(
                    require_source("fed-h15"),
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 1),
                    session=cast(HttpSession, session),
                    context=context,
                )
            fetch.assert_called_once()
            browser.assert_not_called()
