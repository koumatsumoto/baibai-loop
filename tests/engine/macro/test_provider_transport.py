from __future__ import annotations

from datetime import date
from typing import cast
from unittest.mock import MagicMock, patch

from tests.engine.macro.indicator_fixtures import (
    _FakeResponse,
    _RaisingSession,
    _series,
    _StaticSession,
)
from tests.engine.macro.provider_fixtures import SnapshotFixtures

from baibai_engine.macro.indicators.providers import (
    IndicatorsProviderError,
    fetch_observations,
    parse_ecb_fx_csv,
)
from baibai_engine.macro.indicators.providers.base import (
    BrowserUnavailableError,
    FetchContext,
    HttpSession,
    SourceWithheldError,
    fetch_bytes,
    fetch_text,
    store_fetched_bytes,
)


class IndicatorsProviderParserTests(SnapshotFixtures):
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

        from baibai_engine.macro.indicators.providers.browser import BrowserFetcher

        with (
            patch(
                "baibai_engine.macro.indicators.providers.browser.sync_playwright",
                side_effect=PlaywrightError("no browser binary"),
            ),
            self.assertRaisesRegex(BrowserUnavailableError, r"failed to launch headless browser"),
        ):
            BrowserFetcher().fetch_download("https://example.test/data", max_bytes=1000)

    def test_browser_launch_failure_on_a_full_disk_is_still_a_provider_failure(self) -> None:
        from baibai_engine.macro.indicators.providers.browser import BrowserFetcher

        with (
            patch(
                "baibai_engine.macro.indicators.providers.browser.tempfile.TemporaryDirectory",
                side_effect=OSError("No space left on device"),
            ),
            # A bare OSError would escape every handler that knows what a provider
            # failure means, taking the refresh's retry and cache discard with it.
            self.assertRaisesRegex(BrowserUnavailableError, r"failed to launch headless browser"),
        ):
            BrowserFetcher().fetch_download("https://example.test/data", max_bytes=1000)

    def test_browser_launch_receives_only_allowlisted_runtime_environment(self) -> None:
        from baibai_engine.macro.indicators.providers.browser import BrowserFetcher

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
                "baibai_engine.macro.indicators.providers.browser.sync_playwright",
                return_value=starter,
            ),
            patch.dict("os.environ", inherited, clear=True),
            BrowserFetcher() as fetcher,
        ):
            assert fetcher._ensure_context() is context

        launch = playwright.chromium.launch_persistent_context
        assert launch.call_count == 1
        assert launch.call_args.kwargs["env"] == {
            "HOME": "/home/runner",
            "PATH": "/usr/bin",
            "XDG_RUNTIME_DIR": "/run/user/1001",
            "PLAYWRIGHT_BROWSERS_PATH": "/home/runner/.cache/ms-playwright",
        }

    def test_parse_ecb_fx_csv_computes_cross_rate(self) -> None:
        series = _series("ecb_fx", "USDJPY", unit="jpy-per-usd")
        text = "Date,USD,JPY,AUD\n2026-05-08,1.1761,184.37,1.6259\n"

        observations = parse_ecb_fx_csv(
            series,
            text,
            start=date(2026, 5, 8),
            end=date(2026, 5, 8),
        )

        self.assertEqual(len(observations), 1)
        self.assertAlmostEqual(observations[0].value, 156.76, places=2)

    def test_parse_ecb_fx_csv_rejects_missing_required_column(self) -> None:
        series = _series("ecb_fx", "USDJPY", unit="jpy-per-usd")
        text = "Date,JPY\n2026-05-08,184.37\n"

        with self.assertRaisesRegex(IndicatorsProviderError, "missing column"):
            parse_ecb_fx_csv(series, text, start=date(2026, 5, 8), end=date(2026, 5, 8))

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
