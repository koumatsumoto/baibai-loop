from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing, suppress
from functools import partial
from http import HTTPStatus
from typing import TYPE_CHECKING, Literal, Protocol

import requests

from baibai_engine.data_collection.transport.http import (
    AcquisitionDeadlineExceeded,
    check_acquisition_deadline,
    retry_after_delay,
    wait_for_read_retry,
)

if TYPE_CHECKING:
    from baibai_engine.data_collection.transport.browser import BrowserFetcher
    from baibai_engine.data_collection.transport.retry import ReadDeadline

HTTP_TIMEOUT_SECONDS = 30
MAX_CSV_RESPONSE_BYTES = 8_000_000
MAX_ZIP_RESPONSE_BYTES = 16_000_000

# External acquisition purpose: refresh updates a selected range, while rebuild
# reacquires the full history. Ordinary reads do not create a FetchContext.
type FetchPurpose = Literal["refresh", "rebuild"]


class IndicatorsProviderError(RuntimeError):
    """Raised when a source provider cannot acquire requested source input."""


class ProviderFetchStoppedError(IndicatorsProviderError):
    """The provider has exhausted retries or determined that this fetch must stop.

    The service must not restart an exhausted transport, an expired fetch deadline,
    or a permanent response/schema failure reported under this type.
    """


class ProviderRateLimitError(ProviderFetchStoppedError):
    """Stop new requests sharing this acquisition group's quota for the current pass."""


class SourceWithheldError(IndicatorsProviderError):
    """An empty successful response or HTTP 403, kept distinct from other failures.

    This existing response category covers an empty successful response and HTTP
    403. It does not establish that a resource exists, why access was refused, or
    whether another route is permitted or will succeed. Size caps, 404, 429, 5xx
    and network failures remain ordinary provider errors, not fallback signals.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BrowserDownloadTimeoutError(IndicatorsProviderError):
    """No download started; the browser supplies only its bounded navigation outcome."""

    def __init__(self, message: str, *, diagnostic: str) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


class BrowserUnavailableError(IndicatorsProviderError):
    """No browser could be started, so nothing was learned about the source.

    A caller that remembers which sources turned its navigations away must not
    remember this one: the navigation never happened. The next series has the
    same reason to try as this one did.
    """


type BytesCacheKey = tuple[str, tuple[tuple[str, str], ...], bool]


class FetchContext:
    """Shared HTTP session, a per-run bytes cache, and a lazy headless browser.

    One context serves a whole refresh pass. The bytes cache lets bulk-file
    providers (FRB H.15, ECB FX) download one shared file once and reuse it
    across every series that maps to it. The browser starts lazily for callers
    that explicitly need rendered HTML.

    ``purpose`` distinguishes a selected-range refresh from full-history
    reacquisition for providers that need it (see :data:`FetchPurpose`).
    """

    def __init__(self, *, purpose: FetchPurpose = "refresh") -> None:
        self.session = requests.Session()
        self.bytes_cache: dict[BytesCacheKey, bytes] = {}
        self.read_deadlines: dict[str, ReadDeadline] = {}
        self.purpose = purpose
        self._browser: BrowserFetcher | None = None

    def discard_cached_bytes(
        self, url: str, params: Mapping[str, str] | None, *, allow_redirects: bool = True
    ) -> None:
        """Drop only this unusable response, keeping other cached responses."""
        self.bytes_cache.pop(_bytes_cache_key(url, params, allow_redirects=allow_redirects), None)

    def browser_fetcher(self) -> BrowserFetcher:
        self.check_deadline()
        # Lazy import breaks the base <-> browser module cycle and keeps
        # Playwright off the import path until a browser-backed series runs.
        if self._browser is None:
            from baibai_engine.data_collection.transport.browser import BrowserFetcher

            self._browser = BrowserFetcher()
        return self._browser

    def check_deadline(self) -> None:
        try:
            check_acquisition_deadline()
        except AcquisitionDeadlineExceeded as error:
            raise ProviderFetchStoppedError(str(error)) from error

    def close(self) -> None:
        # Releasing resources must never mask the pass's own result, so a failure
        # to close is swallowed here rather than propagating out of the `with`.
        with suppress(OSError):
            self.session.close()
        if self._browser is not None:
            with suppress(OSError):
                self._browser.close()

    def __enter__(self) -> FetchContext:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class HttpSession(Protocol):
    """Structural seam for HTTP fetching so tests can inject fake sessions."""

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: int,
        stream: bool = False,
        allow_redirects: bool = True,
    ) -> requests.Response: ...


def fetch_text(
    session: HttpSession,
    url: str,
    *,
    params: dict[str, str] | None,
    max_bytes: int,
    headers: Mapping[str, str] | None = None,
    context: FetchContext | None = None,
    retry_transient: bool = False,
    allow_redirects: bool = True,
) -> str:
    content = fetch_bytes(
        session,
        url,
        params=params,
        max_bytes=max_bytes,
        headers=headers,
        context=context,
        retry_transient=retry_transient,
        allow_redirects=allow_redirects,
    )
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        # Decoding is not a transport retry. Do not poison later reads of this
        # resource, but retain other successfully received responses in the pass.
        if context is not None:
            context.discard_cached_bytes(url, params, allow_redirects=allow_redirects)
        raise IndicatorsProviderError(f"response from {url} is not UTF-8: {exc}") from exc


def _bytes_cache_key(
    url: str, params: Mapping[str, str] | None, *, allow_redirects: bool = True
) -> BytesCacheKey:
    # A response reached by following redirects cannot prove that the initial
    # URL served the bytes. Strict callers need their own direct receipt.
    return (url, tuple(sorted((params or {}).items())), allow_redirects)


def _guard_response(content: bytes, *, url: str, max_bytes: int, detail: str = "") -> None:
    if not content:
        raise SourceWithheldError(f"empty response body from {url}{detail}")
    if len(content) > max_bytes:
        raise IndicatorsProviderError(f"indicator response too large: {len(content)} bytes")


def fetch_bytes(
    session: HttpSession,
    url: str,
    *,
    params: dict[str, str] | None,
    max_bytes: int,
    headers: Mapping[str, str] | None = None,
    context: FetchContext | None = None,
    retry_transient: bool = False,
    allow_redirects: bool = True,
) -> bytes:
    """Receive one resource; native bulk readers may retry its transient failures once.

    Document readers retain their single-GET policy. FRED owns its separate
    adapter budget, so it does not opt into this retry.
    """
    if context is not None:
        context.check_deadline()
    key = _bytes_cache_key(url, params, allow_redirects=allow_redirects)
    if context is not None and key in context.bytes_cache:
        return context.bytes_cache[key]
    for attempt in range(2 if retry_transient else 1):
        try:
            content = _fetch_bytes_once(
                session,
                url,
                params=params,
                max_bytes=max_bytes,
                headers=headers,
                context=context,
                allow_redirects=allow_redirects,
            )
        except requests.RequestException as exc:
            response = exc.response
            retryable = (
                response is not None and response.status_code in {500, 502, 503, 504}
            ) or isinstance(
                exc,
                (
                    requests.ConnectionError,
                    requests.Timeout,
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ContentDecodingError,
                ),
            )
            if not retry_transient or attempt == 1 or not retryable:
                raise IndicatorsProviderError(f"failed to fetch {url}: {exc}") from exc
            delay = retry_after_delay(
                response.headers.get("Retry-After") if response is not None else None,
                fallback=1.0,
            )
            try:
                wait_for_read_retry(delay)
            except AcquisitionDeadlineExceeded as error:
                raise ProviderFetchStoppedError(str(error)) from error
        else:
            if context is not None:
                context.bytes_cache[key] = content
            return content
    raise AssertionError("unreachable GET retry loop")


def _fetch_bytes_once(
    session: HttpSession,
    url: str,
    *,
    params: dict[str, str] | None,
    max_bytes: int,
    headers: Mapping[str, str] | None,
    context: FetchContext | None,
    allow_redirects: bool,
) -> bytes:
    try:
        check_acquisition_deadline()
        get = session.get if allow_redirects else partial(session.get, allow_redirects=False)
        response = get(
            url, params=params, headers=headers, timeout=HTTP_TIMEOUT_SECONDS, stream=True
        )
        with closing(response):
            if not allow_redirects and 300 <= response.status_code < 400:
                raise ProviderFetchStoppedError(
                    f"source returned HTTP {response.status_code} for {url}; not following redirect"
                )
            _raise_for_response(response, url)
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    parsed_length = int(content_length)
                except ValueError:
                    parsed_length = None
                if parsed_length is not None and parsed_length > max_bytes:
                    raise IndicatorsProviderError(
                        f"indicator response too large: {parsed_length} bytes"
                    )
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if context is not None:
                    context.check_deadline()
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise IndicatorsProviderError(f"indicator response too large: {total} bytes")
                chunks.append(chunk)
            content = b"".join(chunks)
            # No parser has a reading for zero bytes. Keep the empty body out of
            # the cache and record the actual response without guessing its cause.
            _guard_response(
                content,
                url=url,
                max_bytes=max_bytes,
                detail=(
                    f" (status {response.status_code}, "
                    f"content-type {response.headers.get('Content-Type')!r})"
                ),
            )
    except AcquisitionDeadlineExceeded as error:
        raise ProviderFetchStoppedError(str(error)) from error
    return content


def _raise_for_response(response: requests.Response, url: str) -> None:
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            raise ProviderRateLimitError(f"source rate-limited {url}") from exc
        if response.status_code == HTTPStatus.FORBIDDEN:
            # Keep the existing response category without inferring the reason
            # for the refusal or permission to use another route.
            raise SourceWithheldError(
                f"source withheld {url}: {exc}", status_code=response.status_code
            ) from exc
        raise
