from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import cast

from tests.engine.macro.indicator_fixtures import (
    _context_with_browser,
    _FakeBrowserFetcher,
    _FakeResponse,
    _ForbiddenResponse,
    _series,
    _StaticSession,
)
from tests.engine.macro.provider_fixtures import SnapshotFixtures

from baibai_engine.macro.indicators.providers import (
    IndicatorsProviderError,
    parse_fred_csv,
    parse_h15_csv,
)
from baibai_engine.macro.indicators.providers.base import (
    MAX_CSV_RESPONSE_BYTES,
    BrowserUnavailableError,
    HttpSession,
)
from baibai_engine.macro.indicators.providers.frb_h15 import FrbH15Provider
from baibai_engine.macro.indicators.providers.umich_sca import parse_umich_table


class IndicatorsProviderParserTests(SnapshotFixtures):
    def test_parse_fred_csv_filters_range_and_missing_values(self) -> None:
        series = _series("fred_csv", "DGS10")
        text = "observation_date,DGS10\n2026-05-01,4.39\n2026-05-02,.\n2026-05-04,4.45\n"

        observations = parse_fred_csv(
            series,
            text,
            start=date(2026, 5, 2),
            end=date(2026, 5, 5),
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_at, date(2026, 5, 4))
        self.assertEqual(observations[0].value, 4.45)

    def test_parse_umich_table_reads_the_column_for_each_published_month(self) -> None:
        series = _series("umich_sca", "ICS_ALL", unit="index", frequency="monthly")
        # The early history is quarterly, so consecutive rows can skip months.
        text = "Month,YYYY,ICS_ALL\nNovember,1952,86.2\nFebruary,1953,90.7\nJune,2026,49.5\n"

        observations = parse_umich_table(series, text, start=date(1952, 1, 1), end=date(2026, 7, 1))

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(1952, 11, 1), 86.2), (date(1953, 2, 1), 90.7), (date(2026, 6, 1), 49.5)],
        )

    def test_parse_umich_table_filters_to_the_requested_window(self) -> None:
        series = _series("umich_sca", "ICS_ALL", unit="index", frequency="monthly")
        text = "Month,YYYY,ICS_ALL\nApril,2026,49.8\nMay,2026,44.8\nJune,2026,49.5\n"

        observations = parse_umich_table(
            series, text, start=date(2026, 5, 1), end=date(2026, 5, 31)
        )

        self.assertEqual([item.observed_at for item in observations], [date(2026, 5, 1)])

    def test_parse_umich_table_skips_a_month_published_without_a_value(self) -> None:
        series = _series("umich_sca", "ICS_ALL", unit="index", frequency="monthly")
        text = "Month,YYYY,ICS_ALL\nMay,2026,44.8\nJune,2026,\n"

        observations = parse_umich_table(series, text, start=date(2026, 1, 1), end=date(2026, 7, 1))

        self.assertEqual([item.observed_at for item in observations], [date(2026, 5, 1)])

    def test_parse_umich_table_rejects_a_row_that_is_not_a_month_of_a_year(self) -> None:
        series = _series("umich_sca", "ICS_ALL", unit="index", frequency="monthly")
        text = "Month,YYYY,ICS_ALL\nQ2,2026,49.5\n"

        with self.assertRaisesRegex(IndicatorsProviderError, "not a month of a year"):
            parse_umich_table(series, text, start=date(2026, 1, 1), end=date(2026, 7, 1))

    def test_parse_umich_table_rejects_a_missing_value_column(self) -> None:
        series = _series("umich_sca", "ICS_ALL", unit="index", frequency="monthly")
        text = "Month,YYYY,ICE_ALL\nJune,2026,49.5\n"

        with self.assertRaisesRegex(IndicatorsProviderError, "missing column ICS_ALL"):
            parse_umich_table(series, text, start=date(2026, 1, 1), end=date(2026, 7, 1))

    def test_parse_h15_csv_computes_spread_bp(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B-RIFLGFCY02_N.B", unit="bp")
        text = "\n".join(
            [
                '"Series Description","2Y","10Y"',
                '"Time Period","RIFLGFCY02_N.B","RIFLGFCY10_N.B"',
                "2026-05-01,3.88,4.39",
                "2026-05-04,3.95,4.45",
            ]
        )

        observations = parse_h15_csv(
            series,
            text,
            start=date(2026, 5, 1),
            end=date(2026, 5, 4),
        )

        self.assertEqual(len(observations), 2)
        self.assertAlmostEqual(observations[0].value, 51.0)
        self.assertAlmostEqual(observations[1].value, 50.0)

    def test_parse_h15_csv_rejects_missing_required_column(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        text = '"Time Period",OTHER\n2026-05-01,4.39\n'

        with self.assertRaisesRegex(IndicatorsProviderError, "missing column RIFLGFCY10_N.B"):
            parse_h15_csv(series, text, start=date(2026, 5, 1), end=date(2026, 5, 1))

    def test_parse_h15_csv_reports_non_csv_response_with_snippet(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        text = "<!DOCTYPE html>\n<html><head><title>Access Denied</title></head></html>"

        with self.assertRaisesRegex(
            IndicatorsProviderError,
            r"missing Time Period header; response starts with: '<!DOCTYPE html> <html>",
        ):
            parse_h15_csv(series, text, start=date(2026, 5, 1), end=date(2026, 5, 1))

    def test_h15_fetch_sends_browser_user_agent(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
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

        observations = FrbH15Provider().fetch(
            series,
            start=date(2026, 5, 1),
            end=date(2026, 5, 1),
            session=cast(HttpSession, _Session()),
        )

        self.assertEqual(len(observations), 1)
        headers = cast("dict[str, str]", captured["headers"])
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        # Companion browser headers reduce the datacenter-IP block-page rate.
        self.assertIn("Accept", headers)
        self.assertIn("Accept-Language", headers)

    def test_h15_falls_back_to_a_browser_when_the_plain_response_is_empty(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        browser = _FakeBrowserFetcher(b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n')

        with _context_with_browser(browser) as context:
            observations = FrbH15Provider().fetch(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(b""))),
                context=context,
            )

        self.assertEqual(len(observations), 1)
        self.assertAlmostEqual(observations[0].value, 4.39)
        self.assertEqual(len(browser.urls), 1)
        # The browser navigates to the same query the plain client sent.
        self.assertIn("series=bf17364827e38702b42a58cf8eaa3f78", browser.urls[0])
        self.assertIn("from=05%2F01%2F2026", browser.urls[0])

    def test_h15_falls_back_to_a_browser_when_the_plain_response_is_forbidden(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        browser = _FakeBrowserFetcher(b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n')

        with _context_with_browser(browser) as context:
            observations = FrbH15Provider().fetch(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                # A Cloudflare bot-mitigation challenge arrives as a 403 as
                # readily as it arrives as an empty body.
                session=cast(HttpSession, _StaticSession(_ForbiddenResponse())),
                context=context,
            )

        self.assertEqual(len(observations), 1)
        self.assertEqual(len(browser.urls), 1)

    def test_h15_falls_back_to_a_browser_when_the_plain_response_is_a_block_page(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        browser = _FakeBrowserFetcher(b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n')
        block_page = b"<!DOCTYPE html><html><title>Access Denied</title></html>"

        with _context_with_browser(browser) as context:
            observations = FrbH15Provider().fetch(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(block_page))),
                context=context,
            )

        self.assertEqual(len(observations), 1)
        self.assertEqual(len(browser.urls), 1)

    def test_h15_browser_fallback_downloads_once_for_every_series_in_a_pass(self) -> None:
        browser = _FakeBrowserFetcher(
            b'"Time Period","RIFLGFCY10_N.B","RIFLGFCY02_N.B"\n2026-05-01,4.39,3.88\n'
        )
        session = cast(HttpSession, _StaticSession(_FakeResponse(b"")))

        with _context_with_browser(browser) as context:
            for provider_series_id in ("RIFLGFCY10_N.B", "RIFLGFCY02_N.B"):
                FrbH15Provider().fetch(
                    _series("frb_h15", provider_series_id),
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 1),
                    session=session,
                    context=context,
                )

        # The package download is shared, so a second series reads the cache
        # rather than paying for another browser navigation.
        self.assertEqual(len(browser.urls), 1)

    def test_h15_reports_the_plain_failure_when_the_browser_is_also_blocked(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        browser = _FakeBrowserFetcher(IndicatorsProviderError("browser could not download"))

        with (
            _context_with_browser(browser) as context,
            self.assertRaisesRegex(
                IndicatorsProviderError,
                r"browser could not download; the plain client first failed with: "
                r"empty response body from .* \(status 200, content-type 'text/html'\)",
            ),
        ):
            FrbH15Provider().fetch(
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
        series = _series("frb_h15", "RIFLGFCY10_N.B")
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
            FrbH15Provider().fetch(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(block_page))),
                context=context,
            )

    def test_h15_rejects_a_browser_response_without_the_csv_header(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        browser = _FakeBrowserFetcher(b"<!DOCTYPE html><html><title>Just a moment</title></html>")

        with _context_with_browser(browser) as context:
            with self.assertRaisesRegex(
                IndicatorsProviderError, r"browser response without the CSV header"
            ):
                FrbH15Provider().fetch(
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
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        browser = _FakeBrowserFetcher(b"\xff\xfe\x00garbage")

        with _context_with_browser(browser) as context:
            # A decode error is not an IndicatorsProviderError, so escaping as one
            # would deny the refresh its retry and leave the bytes cached.
            with self.assertRaises(IndicatorsProviderError):
                FrbH15Provider().fetch(
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
                with self.assertRaises(IndicatorsProviderError) as caught:
                    FrbH15Provider().fetch(
                        _series("frb_h15", provider_series_id),
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
                with self.assertRaises(IndicatorsProviderError):
                    FrbH15Provider().fetch(
                        _series("frb_h15", provider_series_id),
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
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        browser = _FakeBrowserFetcher(b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n')
        oversized = _FakeResponse(b"x" * 32, headers={"Content-Length": "9000000"})

        with _context_with_browser(browser) as context:
            with self.assertRaisesRegex(IndicatorsProviderError, r"response too large"):
                FrbH15Provider().fetch(
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
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        series = replace(series, source_url="https://example.test/Output.aspx?rel=H15")
        browser = _FakeBrowserFetcher(b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n')

        with _context_with_browser(browser) as context:
            FrbH15Provider().fetch(
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
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        browser = _FakeBrowserFetcher(b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n')

        with _context_with_browser(browser) as context:
            FrbH15Provider().fetch(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(b""))),
                context=context,
            )

        # One resource, one ceiling — not a second one that depends on the route.
        self.assertEqual(browser.max_bytes, [MAX_CSV_RESPONSE_BYTES])

    def test_h15_without_a_context_reports_the_response_instead_of_falling_back(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        block_page = b"<!DOCTYPE html><html><title>Access Denied</title></html>"

        with self.assertRaisesRegex(
            IndicatorsProviderError, r"missing Time Period header; response starts with: '<!DOCTYPE"
        ):
            FrbH15Provider().fetch(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(block_page))),
            )
