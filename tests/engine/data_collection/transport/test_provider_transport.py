from __future__ import annotations

from functools import partial
from typing import cast
from unittest import TestCase
from unittest.mock import MagicMock, patch

import requests
from tests.helpers.provider_fixtures import (
    _FakeResponse,
    _ForbiddenResponse,
    _StaticSession,
)

from baibai_engine.data_collection.transport.context import (
    BrowserUnavailableError,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    SourceWithheldError,
    fetch_bytes,
    fetch_text,
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

    def test_forbidden_and_empty_response_keep_distinct_transport_status(self) -> None:
        for response, status in ((_ForbiddenResponse(), 403), (_FakeResponse(b""), None)):
            with (
                self.subTest(status=status),
                self.assertRaises(SourceWithheldError) as caught,
            ):
                fetch_bytes(
                    cast(HttpSession, _StaticSession(response)),
                    "https://example.test/data",
                    params=None,
                    max_bytes=1000,
                )
            self.assertEqual(caught.exception.status_code, status)

    def test_fetch_text_reports_a_non_utf8_response_as_a_provider_failure(self) -> None:
        session = _StaticSession(_FakeResponse(b"\xff\xfe\x00garbage"))

        # Text decoding is a provider failure, not a reason to resend the GET.
        with self.assertRaisesRegex(IndicatorsProviderError, r"is not UTF-8"):
            fetch_text(
                cast(HttpSession, session),
                "https://example.test/data",
                params=None,
                max_bytes=1000,
            )

    def test_redirect_policies_do_not_share_a_receipt_in_either_order(self) -> None:
        url = "https://example.test/package"
        for first in (True, False):
            with self.subTest(first=first), FetchContext() as context:
                session = MagicMock()
                session.get.side_effect = lambda _url, **kw: _FakeResponse(
                    b"followed" if kw.get("allow_redirects", True) else b"direct"
                )
                for policy in (first, not first, first, not first):
                    self.assertEqual(
                        fetch_bytes(
                            session,
                            url,
                            params=None,
                            max_bytes=1000,
                            context=context,
                            allow_redirects=policy,
                        ),
                        b"followed" if policy else b"direct",
                    )
                self.assertEqual(session.get.call_count, 2)

    def test_decode_failure_only_discards_the_receipt_for_its_redirect_policy(self) -> None:
        url = "https://example.test/package"
        for good in (True, False):
            with self.subTest(good=good), FetchContext() as context:
                session = MagicMock()
                session.get.side_effect = [
                    _FakeResponse(b"good"),
                    _FakeResponse(b"\xff"),
                    _FakeResponse(b"repaired"),
                ]

                read = partial(
                    fetch_text, session, url, params=None, max_bytes=1000, context=context
                )
                self.assertEqual(read(allow_redirects=good), "good")
                with self.assertRaisesRegex(IndicatorsProviderError, "not UTF-8"):
                    read(allow_redirects=not good)
                self.assertEqual(read(allow_redirects=good), "good")
                self.assertEqual(read(allow_redirects=not good), "repaired")
                self.assertEqual(session.get.call_count, 3)

    def test_discard_cached_bytes_keeps_other_responses_and_browser_evidence(self) -> None:
        with FetchContext() as context:
            read = partial(
                fetch_bytes,
                cast(HttpSession, _StaticSession(_FakeResponse(b"ok"))),
                "https://example.test/data",
                max_bytes=1000,
                context=context,
            )
            read(params=None)
            read(params={"month": "1"})
            context.blocked_browser_urls["https://example.test/data"] = "turned away"

            context.discard_cached_bytes("https://example.test/data", None)

            self.assertEqual(
                context.bytes_cache, {("https://example.test/data", (("month", "1"),), True): b"ok"}
            )
            self.assertEqual(
                context.blocked_browser_urls, {"https://example.test/data": "turned away"}
            )

    def test_browser_launch_failure_is_reported_as_the_browser_being_unavailable(self) -> None:
        # The provider's decision not to record a block hangs on this type, so it
        # is fixed where it is raised rather than only where a fake supplies it.
        # Imported here so Playwright stays off the whole suite's import path,
        # the same reason the production launch is lazy.
        from playwright.sync_api import Error as PlaywrightError

        from baibai_engine.data_collection.transport.browser import BrowserFetcher

        with (
            patch(
                "baibai_engine.data_collection.transport.browser.sync_playwright",
                side_effect=PlaywrightError("no browser binary"),
            ),
            self.assertRaisesRegex(BrowserUnavailableError, r"failed to launch headless browser"),
        ):
            BrowserFetcher().fetch_html("https://example.test/data")

    def test_browser_launch_failure_on_a_full_disk_is_still_a_provider_failure(self) -> None:
        from baibai_engine.data_collection.transport.browser import BrowserFetcher

        with (
            patch(
                "baibai_engine.data_collection.transport.browser.tempfile.TemporaryDirectory",
                side_effect=OSError("No space left on device"),
            ),
            # A launch failure must not be mistaken for a refusal by the source.
            self.assertRaisesRegex(BrowserUnavailableError, r"failed to launch headless browser"),
        ):
            BrowserFetcher().fetch_html("https://example.test/data")

    def test_browser_launch_receives_only_allowlisted_runtime_environment(self) -> None:
        from baibai_engine.data_collection.transport.browser import BrowserFetcher

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
                "baibai_engine.data_collection.transport.browser.sync_playwright",
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

    def test_browser_fetch_html_retries_navigation_and_closes_each_page(self) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        from baibai_engine.data_collection.transport.browser import BrowserFetcher

        context = MagicMock()
        timed_out, rendered = MagicMock(), MagicMock()
        context.new_page.side_effect = [timed_out, rendered]
        timed_out.goto.side_effect = PlaywrightTimeoutError("navigation timed out")
        rendered.goto.return_value.status = 200
        rendered.content.return_value = "<html>rendered index</html>"
        url = "https://example.test/index"

        with patch.object(BrowserFetcher, "_ensure_context", return_value=context):
            assert BrowserFetcher().fetch_html(url) == "<html>rendered index</html>"

        assert context.new_page.call_count == 2
        timed_out.content.assert_not_called()
        for page in (timed_out, rendered):
            page.goto.assert_called_once_with(url, wait_until="networkidle", timeout=60_000)
            page.close.assert_called_once_with()

    def test_browser_http_error_never_returns_rendered_index_contents(self) -> None:
        from baibai_engine.data_collection.transport.browser import BrowserFetcher

        for status in (403, 404, 429, 503):
            context = MagicMock()
            page = context.new_page.return_value
            page.goto.return_value.status = status
            page.content.return_value = "<html>old release index</html>"
            with (
                self.subTest(status=status),
                patch.object(BrowserFetcher, "_ensure_context", return_value=context),
                self.assertRaisesRegex(IndicatorsProviderError, f"HTTP {status}"),
            ):
                BrowserFetcher().fetch_html("https://example.test/index")
            context.new_page.assert_called_once_with()
            page.goto.assert_called_once()
            page.content.assert_not_called()
            page.close.assert_called_once_with()
