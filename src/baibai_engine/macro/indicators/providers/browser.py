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

# A real Chromium navigation is required where an edge WAF answers a plain HTTP
# client with a 2xx carrying a challenge page or nothing at all: the S&P Global PMI
# release PDFs, the press-release index the manifest append reads, and the FRB H.15
# CSV when its edge blocks the datacenter IPs a hosted run comes from. Header
# spoofing cannot substitute — these edges also read the TLS handshake, which only a
# real browser produces. A source that merely checks the User-Agent does not need
# this and sends that header from the plain client instead.
# One launch is shared across every browser-backed fetch in a run via FetchContext.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_NAV_TIMEOUT_MS = 60_000
_NAV_ATTEMPTS = 3
_MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024


class BrowserFetcher:
    """Lazily-launched headless Chromium for WAF-gated fetches.

    Each caller needs a different result: the PMI provider downloads a release
    PDF (:meth:`fetch_pdf`), the FRB H.15 provider downloads a CSV
    (:meth:`fetch_download`), and the manifest append reads the press-release
    index (:meth:`fetch_html`). All fall back here after the plain client is
    answered with a challenge page or an empty body.

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

    def fetch_download(self, url: str, *, max_bytes: int = _MAX_DOWNLOAD_BYTES) -> bytes:
        """Navigate to ``url`` and return the downloaded file's bytes.

        For a source that answers with ``Content-Disposition: attachment`` and a
        format carrying no magic bytes of its own (a CSV). Validating what came
        back is the caller's job: only it knows what the file should contain.
        ``max_bytes`` is the caller's own ceiling so one resource does not have
        two different limits depending on which route reached it.

        One navigation, not several. A download either starts as the navigation
        commits or the edge served something else instead, and repeating the
        navigation waits out the timeout again for each retry. The refresh pass
        already retries the whole fetch (:data:`PROVIDER_FETCH_ATTEMPTS`), which
        is where a second chance belongs.
        """

        try:
            return self._fetch_download(url, max_bytes=min(max_bytes, _MAX_DOWNLOAD_BYTES))
        except PlaywrightError as exc:
            raise IndicatorsProviderError(f"browser failed downloading from {url}: {exc}") from exc

    def _fetch_download(self, url: str, *, max_bytes: int) -> bytes:
        context = self._ensure_context()
        page = context.new_page()
        try:
            try:
                with (
                    page.expect_download(timeout=_NAV_TIMEOUT_MS) as download_info,
                    suppress(PlaywrightError),
                ):
                    page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                download = download_info.value
            except PlaywrightTimeoutError as exc:
                raise IndicatorsProviderError(
                    f"browser could not download from {url}: "
                    "navigation timed out before a download started"
                ) from exc
            path = download.path()
            size = Path(path).stat().st_size
            if size > max_bytes:
                # Playwright writes the whole file before handing it over, so the
                # only thing still worth doing is not leaving it on a runner disk
                # the rest of the batch has to share.
                with suppress(PlaywrightError):
                    download.delete()
                raise IndicatorsProviderError(f"browser download exceeds {max_bytes} bytes: {size}")
            return Path(path).read_bytes()
        finally:
            page.close()

    def fetch_pdf(self, url: str) -> bytes:
        """Navigate to ``url`` and return the downloaded PDF bytes."""

        try:
            return self._fetch_pdf(url)
        except PlaywrightError as exc:
            # A crashed browser, a closed page or a failed download read surfaces
            # anywhere in the attempt loop; every one of them must reach the caller
            # as a provider failure so one dead browser cannot abort a refresh pass.
            raise IndicatorsProviderError(f"browser failed fetching PDF from {url}: {exc}") from exc

    def _fetch_pdf(self, url: str) -> bytes:
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

        try:
            return self._fetch_html(url)
        except PlaywrightError as exc:
            raise IndicatorsProviderError(
                f"browser failed fetching HTML from {url}: {exc}"
            ) from exc

    def _fetch_html(self, url: str) -> str:
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
