"""GET retry/body handling shared by the J-Quants and FRED indicator providers."""

from __future__ import annotations

import time
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self, cast

from requests import PreparedRequest, Response, Session
from requests.adapters import HTTPAdapter
from urllib3.connectionpool import ConnectionPool, HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import DecodeError, HTTPError, MaxRetryError, ProtocolError
from urllib3.response import BaseHTTPResponse, HTTPResponse

from baibai_engine.data_collection.transport.context import (
    FetchContext,
    ProviderFetchStoppedError,
    ProviderRateLimitError,
)
from baibai_engine.data_collection.transport.http import (
    AcquisitionDeadlineExceeded,
    check_acquisition_deadline,
    retry_after_delay,
)
from baibai_engine.data_collection.transport.http import (
    ReadRetry as JobReadRetry,
)

_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(slots=True)
class ReadDeadline:
    provider: str
    seconds: float = 20 * 60
    expires_at: float | None = None

    def check(self, waiting: float = 0.0) -> None:
        check_acquisition_deadline(waiting)
        now = time.monotonic()
        if self.expires_at is None:
            self.expires_at = now + self.seconds
        if now + waiting >= self.expires_at:
            raise ProviderFetchStoppedError(f"{self.provider} fetch deadline exhausted")

    def wait(self, seconds: float) -> None:
        self.check(seconds)
        if seconds > 0:
            time.sleep(seconds)
        self.check()


class ReadRetry(JobReadRetry):
    """Share the finite GET backoff, but retain this provider's deadline on clones.

    The job-only receiver and this SDK/body adapter use the same retry spacing.
    Provider deadline and exhausted-quota reporting remain local to this adapter.
    """

    def __init__(self, *, deadline: ReadDeadline, **kwargs: Any) -> None:
        self.deadline = deadline
        super().__init__(**kwargs)

    def new(self, **kw: Any) -> Self:
        return super().new(deadline=self.deadline, **kw)

    def increment(
        self,
        method: str | None = None,
        url: str | None = None,
        response: BaseHTTPResponse | None = None,
        error: Exception | None = None,
        _pool: ConnectionPool | None = None,
        _stacktrace: TracebackType | None = None,
    ) -> Self:
        try:
            return super().increment(method, url, response, error, _pool, _stacktrace)
        except MaxRetryError:
            if response is not None and response.status == 429:
                response.drain_conn()
                raise ProviderRateLimitError(
                    f"{self.deadline.provider} rate-limited (HTTP 429): retries exhausted"
                ) from None
            raise

    def sleep(self, response: BaseHTTPResponse | None = None) -> None:
        waiting = self.get_backoff_time()
        if response is not None and self.respect_retry_after_header:
            waiting = retry_after_delay(response.headers.get("Retry-After"), fallback=waiting)
        try:
            self.deadline.wait(waiting)
        except (ProviderFetchStoppedError, AcquisitionDeadlineExceeded) as exc:
            if response is not None and response.status == 429:
                raise ProviderRateLimitError(
                    f"{self.deadline.provider} rate-limited (HTTP 429): {exc}"
                ) from None
            raise


def read_deadline(provider: str, context: FetchContext | None) -> ReadDeadline:
    """One provider's budget in this pass, with no state carried into a later pass."""
    if context is None:
        return ReadDeadline(provider)
    if provider not in context.read_deadlines:
        context.read_deadlines[provider] = ReadDeadline(provider)
    return context.read_deadlines[provider]


class _ReadPool(HTTPConnectionPool):
    """Read the body while urllib3 still owns this GET's retry state."""

    max_bytes: int | None = None

    def urlopen(self, *args: Any, **kwargs: Any) -> BaseHTTPResponse:
        # urllib3 owns/reclaims the connection until _make_request reads the body.
        kwargs["preload_content"] = True
        kwargs["decode_content"] = True
        return super().urlopen(*args, **kwargs)

    def _make_request(self, conn: Any, *args: Any, **kwargs: Any) -> BaseHTTPResponse:
        # Inspect the status before reading: a body timeout must not retry auth,
        # and a 429 must keep its Retry-After even if its error body is incomplete.
        kwargs["preload_content"] = False
        response = super()._make_request(conn, *args, **kwargs)
        try:
            if self.max_bytes is None:
                response.read(decode_content=True, cache_content=True)
            else:
                length = response.headers.get("Content-Length")
                try:
                    parsed_length = int(length) if length is not None else None
                except ValueError:
                    parsed_length = None
                if parsed_length is not None and parsed_length > self.max_bytes:
                    raise ProviderFetchStoppedError(f"indicator response too large: {length} bytes")
                chunks: list[bytes] = []
                total = 0
                while chunk := response.read(64 * 1024, decode_content=True):
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise ProviderFetchStoppedError(
                            f"indicator response too large: {total} bytes"
                        )
                    chunks.append(chunk)
                # requests consumes the already-read bytes after urllib3 returns.
                cast("HTTPResponse", response)._body = b"".join(chunks)
        except HTTPError as exc:
            if response.status in _RETRYABLE_STATUSES:
                conn.close()
            elif response.status >= 400:
                raise ProviderFetchStoppedError(
                    f"indicator HTTP {response.status} body failed: {type(exc).__name__}"
                ) from None
            elif isinstance(exc, DecodeError):
                # Content decoding is still receipt of this GET's body. urllib3
                # retries ProtocolError, while CSV/JSON parsing stays outside it.
                raise ProtocolError("failed to decode indicator response body", exc) from exc
            else:
                raise
        return response


class _HTTPSReadPool(_ReadPool, HTTPSConnectionPool):
    pass


class ReadAdapter(HTTPAdapter):
    def __init__(
        self, original: HTTPAdapter, deadline: ReadDeadline, *, max_bytes: int | None = None
    ) -> None:
        self.deadline = deadline
        self.max_bytes = max_bytes
        super().__init__(
            pool_connections=original._pool_connections,
            pool_maxsize=original._pool_maxsize,
            pool_block=original._pool_block,
            max_retries=ReadRetry(
                deadline=deadline,
                total=5,
                allowed_methods=frozenset({"GET"}),
                status_forcelist=_RETRYABLE_STATUSES,
            ),
        )
        # Copy the mapping so no SDK/global pool is changed, including proxy pools.
        self.poolmanager.pool_classes_by_scheme = {
            **self.poolmanager.pool_classes_by_scheme,
            "https": _HTTPSReadPool,
            "http": _ReadPool,
        }

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: Any) -> Any:
        manager = super().proxy_manager_for(proxy, **proxy_kwargs)
        manager.pool_classes_by_scheme = {
            **manager.pool_classes_by_scheme,
            "https": _HTTPSReadPool,
            "http": _ReadPool,
        }
        return manager

    def get_connection_with_tls_context(self, *args: Any, **kwargs: Any) -> Any:
        pool = cast("_ReadPool", super().get_connection_with_tls_context(*args, **kwargs))
        pool.max_bytes = self.max_bytes
        return pool

    def build_response(self, req: PreparedRequest, resp: Any) -> Response:
        response = super().build_response(req, resp)
        # requests would otherwise read an already-consumed stream outside retry.
        response._content = resp.data
        response._content_consumed = True
        return response

    def send(self, request: PreparedRequest, *args: Any, **kwargs: Any) -> Response:
        self.deadline.check()
        return super().send(request, *args, **kwargs)


def mount_read_retry(
    session: Session, prefix: str, *, deadline: ReadDeadline, max_bytes: int | None = None
) -> None:
    original = cast("HTTPAdapter", session.get_adapter(prefix))
    if (
        isinstance(original, ReadAdapter)
        and original.deadline is deadline
        and original.max_bytes == max_bytes
    ):
        return
    session.mount(prefix, ReadAdapter(original, deadline, max_bytes=max_bytes))


def jquants_read_client(api_key: str, *, deadline: ReadDeadline) -> Any:
    try:
        import jquantsapi
    except ModuleNotFoundError as exc:
        raise ProviderFetchStoppedError("jquantsapi is not installed") from exc
    client = jquantsapi.ClientV2(api_key=api_key)
    session = client._request_session()
    original = cast("HTTPAdapter", session.adapters["https://"])
    session.mount("https://", ReadAdapter(original, deadline))
    original.close()
    return client
