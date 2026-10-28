from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from types import TracebackType

from playwright.sync_api import BrowserContext
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from baibai_engine.data_collection.transport.context import (
    BrowserUnavailableError,
    IndicatorsProviderError,
)

# Browser-backed fallback for public sources when the plain client yields no
# usable body. A failed plain request does not identify the server's policy or
# guarantee that browser navigation will work.
# One launch is shared across browser-backed fetches in a run via FetchContext.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_NAV_TIMEOUT_MS = 60_000
_NAV_ATTEMPTS = 3
_MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024

# Playwright otherwise passes the entire Python environment to Chromium. The browser
# executes JavaScript from external origins, so its process receives only OS/runtime
# configuration needed to launch and render. Browser discovery happens in the
# Playwright driver before Chromium starts, but keeping PLAYWRIGHT_BROWSERS_PATH makes
# the contract explicit for installations that inspect it again in a child process.
# Proxy variables are intentionally absent: they can embed credentials and the hosted
# runner reaches these public sources directly.
_BROWSER_ENV_ALLOWLIST = frozenset(
    {
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "FONTCONFIG_FILE",
        "FONTCONFIG_PATH",
        "HOME",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LD_LIBRARY_PATH",
        "PATH",
        "PLAYWRIGHT_BROWSERS_PATH",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    }
)


def _browser_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str | float | bool]:
    """Return launch configuration without application credentials."""

    environment = os.environ if source is None else source
    return {name: value for name, value in environment.items() if name in _BROWSER_ENV_ALLOWLIST}


class BrowserFetcher:
    """Shared browser for public downloads and rendered HTML.

    Callers choose whether browser transport is permitted and validate content.
    The PMI receiver uses direct HTTP, not this browser.
    """

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._profile: tempfile.TemporaryDirectory[str] | None = None

    def _ensure_context(self) -> BrowserContext:
        if self._context is not None:
            return self._context
        try:
            self._profile = tempfile.TemporaryDirectory(prefix="baibai-indicators-browser-")
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
                env=_browser_environment(),
            )
        except (PlaywrightError, OSError) as exc:
            # The profile directory is written here too, so a full or read-only
            # disk fails the launch as surely as Playwright does. Both are "no
            # browser started", and leaving one of them as a bare OSError puts it
            # outside every handler that knows what a provider failure means.
            self.close()
            raise BrowserUnavailableError(f"failed to launch headless browser: {exc}") from exc
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
            # A browser that died mid-navigation reports on itself, not on the
            # source, so it is the same kind of failure as one that never started
            # and must not be quoted as evidence of how a source behaved. The dead
            # context goes with it; the next caller launches a fresh one.
            self.close()
            raise BrowserUnavailableError(f"browser failed downloading from {url}: {exc}") from exc

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

    def fetch_pdf(self, url: str, *, max_bytes: int = _MAX_DOWNLOAD_BYTES) -> bytes:
        """Read one download and require PDF bytes; the refresh service owns retries."""

        content = self.fetch_download(url, max_bytes=max_bytes)
        if not content.startswith(b"%PDF"):
            raise IndicatorsProviderError(f"browser download from {url} is not a PDF")
        return content

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
