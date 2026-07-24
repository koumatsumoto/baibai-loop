from __future__ import annotations

import json
import tempfile
from contextlib import suppress
from pathlib import Path
from types import TracebackType

from playwright.sync_api import Browser, BrowserContext, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .base import IndicatorsProviderError

# A real Chromium navigation is required for sources whose edge WAF gates plain
# HTTP clients (S&P Global PMI PDFs, Nikkei / NBS valuation pages). One launch is
# shared across every browser-backed series in a run via FetchContext.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_NAV_TIMEOUT_MS = 60_000
_NAV_ATTEMPTS = 3
_MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024


class BrowserFetcher:
    """Lazily-launched headless Chromium for WAF-gated fetches (PDF and HTML).

    The browser starts on first use and is reused for every fetch in the run, so
    a batch that refreshes several browser-backed series pays one launch. All
    Playwright errors surface as :class:`IndicatorsProviderError` so the CLI and
    daily batch never leak a raw traceback and treat a browser failure like any
    other provider failure (retryable / deferred).
    """

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._profile: tempfile.TemporaryDirectory[str] | None = None

    def _ensure_context(self) -> BrowserContext:
        if self._context is not None:
            return self._context
        try:
            self._profile = tempfile.TemporaryDirectory(prefix="baibai-macro-browser-")
            preferences = Path(self._profile.name) / "Default" / "Preferences"
            preferences.parent.mkdir(parents=True, exist_ok=True)
            # Force PDFs to download rather than open in the viewer so the bytes
            # are reachable via expect_download.
            preferences.write_text(
                json.dumps(
                    {
                        "download": {"prompt_for_download": False},
                        "plugins": {"always_open_pdf_externally": True},
                    }
                ),
                encoding="utf-8",
            )
            self._playwright = sync_playwright().start()
            self._context = self._playwright.chromium.launch_persistent_context(
                self._profile.name,
                headless=True,
                accept_downloads=True,
                user_agent=_USER_AGENT,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except PlaywrightError as exc:
            self.close()
            raise IndicatorsProviderError(f"failed to launch headless browser: {exc}") from exc
        return self._context

    def fetch_pdf(self, url: str) -> bytes:
        """Navigate to ``url`` and return the downloaded PDF bytes."""

        context = self._ensure_context()
        last_error = "no download started"
        for _attempt in range(_NAV_ATTEMPTS):
            page = context.new_page()
            try:
                try:
                    with (
                        page.expect_download(timeout=_NAV_TIMEOUT_MS) as download_info,
                        suppress(PlaywrightError),
                    ):
                        page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                    download = download_info.value
                except PlaywrightTimeoutError:
                    last_error = "navigation timed out before a download started"
                    continue
                path = download.path()
                size = Path(path).stat().st_size
                if size > _MAX_DOWNLOAD_BYTES:
                    raise IndicatorsProviderError(
                        f"browser PDF download exceeds {_MAX_DOWNLOAD_BYTES} bytes: {size}"
                    )
                content = Path(path).read_bytes()
                if not content.startswith(b"%PDF"):
                    last_error = "downloaded file is not a PDF"
                    continue
                return content
            finally:
                page.close()
        raise IndicatorsProviderError(f"browser could not fetch PDF from {url}: {last_error}")

    def fetch_html(self, url: str) -> str:
        """Navigate to ``url`` and return the fully rendered HTML."""

        context = self._ensure_context()
        last_error = "navigation failed"
        for _attempt in range(_NAV_ATTEMPTS):
            page = context.new_page()
            try:
                try:
                    page.goto(url, wait_until="networkidle", timeout=_NAV_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    last_error = "navigation timed out"
                    continue
                except PlaywrightError as exc:
                    last_error = str(exc)
                    continue
                return page.content()
            finally:
                page.close()
        raise IndicatorsProviderError(f"browser could not fetch HTML from {url}: {last_error}")

    def close(self) -> None:
        if self._context is not None:
            with suppress(PlaywrightError):
                self._context.close()
            self._context = None
        if self._browser is not None:
            with suppress(PlaywrightError):
                self._browser.close()
            self._browser = None
        if self._playwright is not None:
            with suppress(PlaywrightError):
                self._playwright.stop()
            self._playwright = None
        if self._profile is not None:
            self._profile.cleanup()
            self._profile = None

    def __enter__(self) -> BrowserFetcher:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
