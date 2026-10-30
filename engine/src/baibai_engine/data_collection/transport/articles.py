"""Bounded article GET envelopes; existing economic bytes APIs remain unchanged.

Redirects are deliberately not followed. Every attempted GET yields a receipt;
no authentication/403/parse failure is retried. An orphan 304 permits exactly
one unconditional recovery GET. Cooldown is returned for Jobs' cross-slot state,
not claimed to be a distributed rate limit.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import requests

from baibai_engine.data_collection.receipts.articles.contracts import ArticleReceipt, BodyRef
from baibai_engine.data_collection.transport.http import (
    AcquisitionDeadlineExceeded,
    check_acquisition_deadline,
    retry_after_delay,
    wait_for_read_retry,
)


@dataclass
class ArticleBudget:
    max_requests: int
    max_bytes: int
    deadline: float
    requests_used: int = 0
    bytes_used: int = 0
    min_interval_seconds: float = 0.0
    last_request_started: float | None = None

    @classmethod
    def start(
        cls, *, max_requests: int, max_bytes: int, seconds: float, min_interval_seconds: float = 0.0
    ) -> ArticleBudget:
        if min(max_requests, max_bytes, seconds) <= 0 or min_interval_seconds < 0:
            raise ValueError("article budgets must be positive")
        return cls(
            max_requests,
            max_bytes,
            time.monotonic() + seconds,
            min_interval_seconds=min_interval_seconds,
        )

    def check(self) -> None:
        check_acquisition_deadline()
        if time.monotonic() >= self.deadline:
            raise ArticleBudgetExceeded("deadline")

    def request(self) -> None:
        self.check()
        if self.requests_used >= self.max_requests:
            raise ArticleBudgetExceeded("requests")
        if self.last_request_started is not None:
            delay = max(
                0.0, self.last_request_started + self.min_interval_seconds - time.monotonic()
            )
            if time.monotonic() + delay >= self.deadline:
                raise ArticleBudgetExceeded("deadline")
            wait_for_read_retry(delay)
        self.last_request_started = time.monotonic()
        self.requests_used += 1

    def consume(self, count: int, *, response_bytes: int, max_response_bytes: int) -> None:
        self.bytes_used += count
        self.check()
        if self.bytes_used > self.max_bytes or response_bytes > max_response_bytes:
            raise ArticleBudgetExceeded("bytes")


class ArticleBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class ArticleResponse:
    receipt: ArticleReceipt
    body: bytes | None = field(repr=False)


def fetch_article(
    session: requests.Session,
    *,
    source: str,
    url: str,
    allowed_hosts: frozenset[str],
    budget: ArticleBudget,
    max_response_bytes: int,
    baseline: ArticleReceipt | None = None,
    representation: str = "default",
) -> tuple[ArticleResponse, ...]:
    """Return actual attempts; caller durably retains each before advancing planning.

    baseline must be a concrete 200 whose closure is available to the target
    receipt store. Passing no baseline sends no validators. It never treats a 304
    as an empty source response, nor attaches it to a similar canonical URL.
    """
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts or parsed.username:
        raise ValueError("article request is outside the explicit HTTPS source allowlist")
    if baseline is not None and (
        baseline.status != "ok"
        or baseline.body_ref is None
        or (baseline.source, baseline.requested_url, baseline.representation)
        != (source, url, representation)
    ):
        raise ValueError("article baseline must be this representation's concrete 200")
    attempts = []
    for recovery in range(2):
        headers = {
            "Accept": "application/json, application/xml, text/html, application/pdf;q=0.9, */*;q=0.5"
        }
        if baseline is not None:
            if baseline.etag:
                headers["If-None-Match"] = baseline.etag
            if baseline.last_modified:
                headers["If-Modified-Since"] = baseline.last_modified
        try:
            budget.request()
        except (ArticleBudgetExceeded, AcquisitionDeadlineExceeded):
            if attempts:
                break
            raise
        received_at = datetime.now(UTC)
        identity = str(uuid.uuid4())
        fields: dict[str, object] = {}
        try:
            with session.get(
                url,
                headers=headers,
                stream=True,
                allow_redirects=False,
                timeout=max(0.1, min(30.0, budget.deadline - time.monotonic())),
            ) as response:
                received_at = datetime.now(UTC)
                fields = {
                    "http_status": response.status_code,
                    "media_type": response.headers.get("Content-Type"),
                    "encoding": response.encoding,
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"),
                    "retry_after": response.headers.get("Retry-After"),
                }
                if response.status_code in (429, 503):
                    delay = retry_after_delay(response.headers.get("Retry-After"), fallback=60.0)
                    fields["cooldown_until"] = received_at + timedelta(seconds=delay)
                body = None
                ref = None
                error = None
                if response.status_code == 200:
                    chunks = []
                    size = 0
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        size += len(chunk)
                        budget.consume(
                            len(chunk), response_bytes=size, max_response_bytes=max_response_bytes
                        )
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    charset = re.search(
                        r"charset\s*=\s*[\"\']?([\w-]+)",
                        response.headers.get("Content-Type", ""),
                        re.I,
                    )
                    if charset is None:
                        charset = re.search(
                            r"charset\s*=\s*[\"\']?([\w-]+)",
                            body[:4096].decode("ascii", errors="ignore"),
                            re.I,
                        )
                    fields["encoding"] = charset.group(1) if charset else "utf-8"
                    ref = BodyRef(receipt_id=identity, sha256=hashlib.sha256(body).hexdigest())
                    status = "ok"
                elif response.status_code == 304 and baseline is not None:
                    fields["encoding"] = baseline.encoding
                    ref = baseline.body_ref
                    status = "not_modified"
                else:
                    status = "http_error"
                    error = (
                        "missing_304_baseline"
                        if response.status_code == 304
                        else f"http_{response.status_code}"
                    )
                receipt = ArticleReceipt.model_validate(
                    {
                        "receipt_id": identity,
                        "source": source,
                        "requested_url": url,
                        "representation": representation,
                        "received_at": received_at,
                        "status": status,
                        "body_ref": ref,
                        "error_code": error,
                        **fields,
                    }
                )
        except requests.RequestException as exc:
            body = None
            receipt = ArticleReceipt.model_validate(
                {
                    "receipt_id": identity,
                    "source": source,
                    "requested_url": url,
                    "representation": representation,
                    "received_at": received_at,
                    "status": "transport_error",
                    "error_code": type(exc).__name__,
                    **fields,
                }
            )
        except (ArticleBudgetExceeded, AcquisitionDeadlineExceeded) as exc:
            body = None
            receipt = ArticleReceipt.model_validate(
                {
                    "receipt_id": identity,
                    "source": source,
                    "requested_url": url,
                    "representation": representation,
                    "received_at": received_at,
                    "status": "budget_exceeded",
                    "error_code": str(exc),
                    **fields,
                }
            )
        attempts.append(ArticleResponse(receipt, body))
        if receipt.error_code != "missing_304_baseline" or recovery:
            break
    return tuple(attempts)
