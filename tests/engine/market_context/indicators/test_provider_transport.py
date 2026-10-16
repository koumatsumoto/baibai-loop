from __future__ import annotations

from datetime import date
from typing import cast
from unittest import TestCase
from unittest.mock import MagicMock, patch

import requests
from tests.engine.market_context.indicators.indicator_fixtures import (
    _FakeResponse,
    _RaisingSession,
    _series,
    _StaticSession,
)

from baibai_engine.market_context.indicators.providers import (
    IndicatorsProviderError,
    fetch_observations,
    parse_ecb_reference_csv,
)
from baibai_engine.market_context.indicators.providers.base import (
    BrowserUnavailableError,
    FetchContext,
    HttpSession,
    SourceWithheldError,
    fetch_bytes,
    fetch_text,
    store_fetched_bytes,
)


class IndicatorsProviderParserTests(TestCase):
    def test_fetch_bytes_closes_successful_response_before_returning(self) -> None:
        response = _FakeResponse(b"ok")
        result = fetch_bytes(
            cast(HttpSession, _StaticSession(response)),
            "https://example.test/data",
            params=None,
            max_bytes=1000,
        )
        self.assertEqual(result, b"ok")
        self.assertTrue(response.closed)

    def test_fetch_bytes_closes_response_when_rejecting_body(self) -> None:
        for response in (
            _FakeResponse(b""),
            _FakeResponse(b"x", headers={"Content-Length": "1001"}),
            _FakeResponse(b"x" * 1001),
        ):
            with self.subTest(headers=response.headers, size=len(response.content)):
                with self.assertRaises(IndicatorsProviderError):
                    fetch_bytes(
                        cast(HttpSession, _StaticSession(response)),
                        "https://example.test/data",
                        params=None,
                        max_bytes=1000,
                    )
                self.assertTrue(response.closed)

    def test_fetch_bytes_closes_response_on_http_or_stream_failure(self) -> None:
        for method in ("raise_for_status", "iter_content"):
            response = _FakeResponse(b"ok")
            with self.subTest(method=method):
                with (
                    patch.object(response, method, side_effect=requests.ConnectionError("lost")),
                    self.assertRaisesRegex(IndicatorsProviderError, "failed to fetch"),
                ):
                    fetch_bytes(
                        cast(HttpSession, _StaticSession(response)),
                        "https://example.test/data",
                        params=None,
                        max_bytes=1000,
                    )
                self.assertTrue(response.closed)

    def test_fetch_bytes_rejects_an_empty_body_naming_status_and_content_type(self) -> None:
        session = _StaticSession(
            _FakeResponse(b"", headers={"Content-Type": "text/html"}, status_code=200)
        )

        with self.assertRaisesRegex(
            IndicatorsProviderError,
            r"empty response body from https://example\.test/data "
            r"\(status 200, content-type 'text/html'\)",
        ):
            fetch_bytes(
                cast(HttpSession, session),
                "https://example.test/data",
                params=None,
                max_bytes=1000,
            )

    def test_fetch_bytes_keeps_an_empty_body_out_of_the_shared_cache(self) -> None:
        session = _StaticSession(_FakeResponse(b""))

        with FetchContext() as context:
            with self.assertRaises(SourceWithheldError):
                fetch_bytes(
                    cast(HttpSession, session),
                    "https://example.test/data",
                    params=None,
                    max_bytes=1000,
                    context=context,
                )

            # A cached empty body would fail every later series sharing the URL
            # without ever re-requesting it.
            self.assertEqual(context.bytes_cache, {})

    def test_fetch_text_reports_a_non_utf8_response_as_a_provider_failure(self) -> None:
        session = _StaticSession(_FakeResponse(b"\xff\xfe\x00garbage"))

        # A raw UnicodeDecodeError escapes the refresh's retry, which only knows
        # about provider errors, leaving the bytes cached for every later series.
        with self.assertRaisesRegex(IndicatorsProviderError, r"is not UTF-8"):
            fetch_text(
                cast(HttpSession, session),
                "https://example.test/data",
                params=None,
                max_bytes=1000,
            )

    def test_store_fetched_bytes_applies_the_guards_the_plain_path_applies(self) -> None:
        with FetchContext() as context:
            # A cache entry is read without any guard in front of it, so bytes
            # reaching the cache by another route owe the same checks.
            with self.assertRaises(SourceWithheldError):
                store_fetched_bytes(context, "https://example.test/data", None, b"", max_bytes=1000)
            with self.assertRaisesRegex(IndicatorsProviderError, r"response too large"):
                store_fetched_bytes(
                    context, "https://example.test/data", None, b"x" * 1001, max_bytes=1000
                )

            self.assertEqual(context.bytes_cache, {})

            store_fetched_bytes(context, "https://example.test/data", None, b"ok", max_bytes=1000)

            self.assertEqual(
                fetch_bytes(
                    cast(HttpSession, _RaisingSession()),
                    "https://example.test/data",
                    params=None,
                    max_bytes=1000,
                    context=context,
                ),
                b"ok",
            )

    def test_discard_cached_bytes_keeps_the_blocked_browser_record(self) -> None:
        with FetchContext() as context:
            store_fetched_bytes(context, "https://example.test/data", None, b"ok", max_bytes=1000)
            context.blocked_browser_urls["https://example.test/data"] = "turned away"

            context.discard_cached_bytes()

            # A retry is worth re-testing the plain request, not a navigation that
            # already spent a timeout failing against the same edge.
            self.assertEqual(context.bytes_cache, {})
            self.assertEqual(
                context.blocked_browser_urls, {"https://example.test/data": "turned away"}
            )

    def test_browser_launch_failure_is_reported_as_the_browser_being_unavailable(self) -> None:
        # The provider's decision not to record a block hangs on this type, so it
        # is fixed where it is raised rather than only where a fake supplies it.
        # Imported here so Playwright stays off the whole suite's import path,
        # the same reason the production launch is lazy.
        from playwright.sync_api import Error as PlaywrightError

        from baibai_engine.market_context.indicators.providers.browser import BrowserFetcher

        with (
            patch(
                "baibai_engine.market_context.indicators.providers.browser.sync_playwright",
                side_effect=PlaywrightError("no browser binary"),
            ),
            self.assertRaisesRegex(BrowserUnavailableError, r"failed to launch headless browser"),
        ):
            BrowserFetcher().fetch_download("https://example.test/data", max_bytes=1000)

    def test_browser_launch_failure_on_a_full_disk_is_still_a_provider_failure(self) -> None:
        from baibai_engine.market_context.indicators.providers.browser import BrowserFetcher

        with (
            patch(
                "baibai_engine.market_context.indicators.providers.browser.tempfile.TemporaryDirectory",
                side_effect=OSError("No space left on device"),
            ),
            # A bare OSError would escape every handler that knows what a provider
            # failure means, taking the refresh's retry and cache discard with it.
            self.assertRaisesRegex(BrowserUnavailableError, r"failed to launch headless browser"),
        ):
            BrowserFetcher().fetch_download("https://example.test/data", max_bytes=1000)

    def test_browser_launch_receives_only_allowlisted_runtime_environment(self) -> None:
        from baibai_engine.market_context.indicators.providers.browser import BrowserFetcher

        context = MagicMock()
        playwright = MagicMock()
        playwright.chromium.launch_persistent_context.return_value = context
        starter = MagicMock()
        starter.start.return_value = playwright
        inherited = {
            "HOME": "/home/runner",
            "PATH": "/usr/bin",
            "XDG_RUNTIME_DIR": "/run/user/1001",
            "PLAYWRIGHT_BROWSERS_PATH": "/home/runner/.cache/ms-playwright",
            "JQUANTS_API_KEY": "jquants-secret",
            "EDINET_API_KEY": "edinet-secret",
            "R2_SECRET_ACCESS_KEY": "r2-secret",
            "FUTURE_VENDOR_TOKEN": "future-secret",
            "HTTPS_PROXY": "https://proxy-user:proxy-secret@example.test",
        }

        with (
            patch(
                "baibai_engine.market_context.indicators.providers.browser.sync_playwright",
                return_value=starter,
            ),
            patch.dict("os.environ", inherited, clear=True),
            BrowserFetcher() as fetcher,
        ):
            assert fetcher._ensure_context() is context

        launch = playwright.chromium.launch_persistent_context
        assert launch.call_count == 1
        context.close.assert_called_once_with()
        playwright.stop.assert_called_once_with()
        assert launch.call_args.kwargs["env"] == {
            "HOME": "/home/runner",
            "PATH": "/usr/bin",
            "XDG_RUNTIME_DIR": "/run/user/1001",
            "PLAYWRIGHT_BROWSERS_PATH": "/home/runner/.cache/ms-playwright",
        }

    def test_pdf_uses_the_shared_download_with_the_callers_limit(self) -> None:
        from baibai_engine.market_context.indicators.providers.browser import BrowserFetcher

        fetcher = BrowserFetcher()
        with patch.object(fetcher, "fetch_download", return_value=b"%PDF-test") as download:
            self.assertEqual(
                fetcher.fetch_pdf("https://example.test/release.pdf", max_bytes=1234),
                b"%PDF-test",
            )
        download.assert_called_once_with("https://example.test/release.pdf", max_bytes=1234)

    def test_pdf_does_not_repeat_a_non_pdf_download_inside_the_provider_retry(self) -> None:
        from baibai_engine.market_context.indicators.providers.browser import BrowserFetcher

        fetcher = BrowserFetcher()
        with (
            patch.object(
                fetcher, "fetch_download", return_value=b"<html>not a PDF</html>"
            ) as download,
            self.assertRaisesRegex(IndicatorsProviderError, "is not a PDF"),
        ):
            fetcher.fetch_pdf("https://example.test/release.pdf", max_bytes=1234)
        download.assert_called_once_with("https://example.test/release.pdf", max_bytes=1234)

    def test_pdf_preserves_the_shared_download_failure_without_retrying(self) -> None:
        from baibai_engine.market_context.indicators.providers.browser import BrowserFetcher

        fetcher = BrowserFetcher()
        failure = BrowserUnavailableError("browser closed")
        with (
            patch.object(fetcher, "fetch_download", side_effect=failure) as download,
            self.assertRaises(BrowserUnavailableError) as raised,
        ):
            fetcher.fetch_pdf("https://example.test/release.pdf", max_bytes=1234)
        self.assertIs(raised.exception, failure)
        download.assert_called_once_with("https://example.test/release.pdf", max_bytes=1234)

    def test_parse_ecb_reference_csv_preserves_all_native_currencies(self) -> None:
        text = "Date,USD,JPY,AUD,CHF\n2026-05-08,1.1761,184.37,1.6259,N/A\n"
        records = parse_ecb_reference_csv(text, start=date(2026, 5, 8), end=date(2026, 5, 8))
        self.assertEqual(
            records[date(2026, 5, 8)][0],
            {
                "Date": "2026-05-08",
                "USD": "1.1761",
                "JPY": "184.37",
                "AUD": "1.6259",
                "CHF": "N/A",
            },
        )

    def test_parse_ecb_reference_csv_rejects_missing_date_column(self) -> None:
        with self.assertRaisesRegex(IndicatorsProviderError, "missing Date column"):
            parse_ecb_reference_csv("JPY\n184.37\n", start=date(2026, 5, 8), end=date(2026, 5, 8))

    def test_fetch_observations_wraps_request_exception(self) -> None:
        series = _series("fred_csv", "DGS10")

        with self.assertRaisesRegex(IndicatorsProviderError, "failed to fetch"):
            fetch_observations(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=_RaisingSession(),
            )

    def test_fetch_observations_rejects_oversized_response(self) -> None:
        series = _series("fred_csv", "DGS10")

        with self.assertRaisesRegex(IndicatorsProviderError, "too large"):
            fetch_observations(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=_StaticSession(
                    _FakeResponse(
                        b"",
                        headers={"Content-Length": "9000000"},
                    )
                ),
            )

    def test_fetch_observations_rejects_unknown_provider(self) -> None:
        series = _series("nonexistent_provider", "X")

        with self.assertRaisesRegex(
            IndicatorsProviderError, "unsupported indicator provider: nonexistent_provider"
        ):
            fetch_observations(series, start=date(2026, 5, 1), end=date(2026, 5, 1))
