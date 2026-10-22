"""Bounded public-equity HTTP receipt transport, independent of HTML interpretation."""

from __future__ import annotations

import re
import signal
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from types import FrameType
from typing import Any, cast
from urllib.parse import urlsplit

import requests

from baibai_engine.collection.equity_web.models import EquityWebSourceResponse, Role, Source
from baibai_engine.collection.equity_web.receipts import validate_response

MIN_REQUEST_INTERVAL_SECONDS = 2.0
# Pair policy changes require a version review in external_assessment/observations/identity.py.
HOST_REQUEST_RATE_PER_SECOND = 2.0
HOST_REQUEST_BURST_CREDITS = 4.0
RATE_LIMITED_HOSTS = (
    "scouter.monex.co.jp",
    "www.rakuten-sec.co.jp",
    "rakuten-sec.ifis.co.jp",
    "jp.kabumap.com",
    "minkabu.jp",
)


class HostRateLimiter:
    """Fixed per-host token buckets shared by serial daily pairs and retries.

    All hosts start with zero credits. A new cohort/process loses unused credits;
    it never creates restart burst credit. Actual acquisition stays serial (one
    request at a time), within the requested maximum concurrency of two.
    """

    def __init__(self) -> None:
        started = time.monotonic()
        self._credits = dict.fromkeys(RATE_LIMITED_HOSTS, 0.0)
        self._updated = dict.fromkeys(RATE_LIMITED_HOSTS, started)

    def wait(self, url: str | bytes, *, deadline: float) -> None:
        if threading.current_thread() is not threading.main_thread():
            raise ValueError("host rate limiter requires the serial main thread")
        if not isinstance(url, str):
            raise ValueError("host rate limiter requires a fixed public HTTPS host")
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise ValueError("host rate limiter requires a fixed public HTTPS host") from None
        if parsed.scheme != "https" or parsed.netloc not in RATE_LIMITED_HOSTS:
            raise ValueError("host rate limiter requires a fixed public HTTPS host")
        host = parsed.netloc
        rate = HOST_REQUEST_RATE_PER_SECOND
        burst = HOST_REQUEST_BURST_CREDITS
        while True:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                raise TimeLimit
            credits = min(
                burst,
                self._credits[host] + (now - self._updated[host]) * rate,
            )
            self._updated[host] = now
            self._credits[host] = credits
            if credits >= 1.0:
                self._credits[host] -= 1.0
                return
            time.sleep(min((1.0 - credits) / rate, remaining))


class TimeLimit(Exception):
    pass


@contextmanager
def wall_clock_limit(seconds: float) -> Iterator[None]:
    # requests' read timeout alone permits a indefinitely trickling response.
    # Linux CLI execution has an interruptible main thread and a real deadline.
    if threading.current_thread() is not threading.main_thread():
        raise ValueError("bounded collection requires the main thread")
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise ValueError("bounded collection requires an unused wall-clock timer")

    def expired(_signum: int, _frame: FrameType | None) -> None:
        raise TimeLimit

    previous = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _http_retry_date(value: str, received: datetime) -> tuple[datetime | None, bool]:
    weekday = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
    month = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    clock = r"[0-9]{2}:[0-9]{2}:(?P<second>[0-9]{2})"
    imf = re.fullmatch(rf"{weekday}, [0-9]{{2}} {month} [0-9]{{4}} {clock} GMT", value)
    rfc850 = re.fullmatch(
        rf"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), "
        rf"[0-9]{{2}}-{month}-(?P<year>[0-9]{{2}}) {clock} GMT",
        value,
    )
    asctime = re.fullmatch(rf"{weekday} {month} (?:[0-9]{{2}}| [0-9]) {clock} [0-9]{{4}}", value)
    matched = imf or rfc850 or asctime
    if matched is None:
        return None, False
    leap_second = matched["second"] == "60"
    if leap_second:
        # Validate the remaining calendar fields with the adjacent representable
        # second, then fail closed: datetime cannot represent an HTTP leap second.
        value = value[: matched.start("second")] + "59" + value[matched.end("second") :]
    try:
        date = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None, False
    if asctime:
        date = date.replace(tzinfo=UTC)
    if rfc850:
        # HTTP's fifty-year window is relative to receipt, not email's fixed
        # 1969/2068 pivot. Compare full dates to cover the boundary within a year.
        year = received.year // 100 * 100 + int(rfc850["year"])
        suffix = (date.month, date.day, date.hour, date.minute, date.second, 0)
        received_suffix = (
            received.month,
            received.day,
            received.hour,
            received.minute,
            received.second,
            received.microsecond,
        )
        if (year, *suffix) < (received.year - 50, *received_suffix):
            year += 100
        if (year, *suffix) > (received.year + 50, *received_suffix):
            year -= 100
        if not 1 <= year <= 9999:
            return None, True
        try:
            date = date.replace(year=year)
        except ValueError:
            return None, False
    if leap_second:
        return None, True
    return max(received, date.astimezone(UTC)), False


def _retry_after(value: str | None, received: datetime) -> tuple[datetime | None, bool]:
    """Parse a confirmed 429 deadline; never shorten a valid overflowing delay."""
    if value is None:
        return None, False
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        # Compare decimal strings before int() so arbitrarily large valid delays
        # fail closed rather than hitting Python's integer-string size limit.
        digits = value.lstrip("0") or "0"
        available = datetime.max.replace(tzinfo=UTC) - received
        maximum = str(available.days * 86400 + available.seconds)
        if len(digits) > len(maximum) or (len(digits) == len(maximum) and digits > maximum):
            return None, True
        return received + timedelta(seconds=int(digits)), False
    return _http_retry_date(value, received)


class ResponseEncodingError(Exception):
    """The retained response body could not be decoded with its declared encoding."""


class HttpFailure(Exception):
    def __init__(
        self,
        status: int,
        retry_after_not_before_utc: datetime | None = None,
        *,
        retry_after_unrepresentable: bool = False,
    ) -> None:
        self.status = status
        self.retry_after_not_before_utc = retry_after_not_before_utc
        self.retry_after_unrepresentable = retry_after_unrepresentable


def receive_html(
    session: requests.Session,
    url: str,
    timeout: float,
    *,
    attempt_id: str,
    source: Source,
    ticker: str,
    role: Role,
    parent_receipt_id: str | None = None,
    on_received_html: Callable[[EquityWebSourceResponse], None] | None = None,
) -> tuple[str, datetime, str | None]:
    # Redirects would add unbudgeted requests and can redirect to login pages.
    with session.get(url, timeout=timeout, allow_redirects=False) as response:
        if response.status_code != 200:
            if response.status_code == 429:
                # PairSession records full body completion before a possibly
                # slow on_response callback. Do not add its delay.
                received = (
                    session.finished_at_utc
                    if isinstance(session, PairSession)
                    else datetime.now(UTC)
                )
                assert received is not None
                deadline, unrepresentable = _retry_after(
                    response.headers.get("Retry-After"), received
                )
                raise HttpFailure(429, deadline, retry_after_unrepresentable=unrepresentable)
            raise HttpFailure(response.status_code)
        body = response.content
        received = (
            session.finished_at_utc if isinstance(session, PairSession) else datetime.now(UTC)
        )
        assert received is not None
        content_type = response.headers.get("Content-Type", "")
        charset = re.search(r"charset\s*=\s*['\"]?([\w-]+)", content_type, re.I)
        meta = re.search(rb"charset\s*=\s*['\"]?([\w-]+)", body[:4096], re.I)
        encoding = charset[1] if charset else meta[1].decode("ascii") if meta else "utf-8"
        receipt_id = None
        if on_received_html is not None:
            prepared = cast("requests.PreparedRequest | None", response.request)
            if prepared is not None and "Authorization" in prepared.headers:
                raise RuntimeError("raw_capture_requires_anonymous_response")
            receipt_id = str(uuid.uuid4())
            original = EquityWebSourceResponse(
                receipt_id=receipt_id,
                attempt_id=attempt_id,
                source=source,
                requested_ticker=ticker,
                role=role,
                parent_receipt_id=parent_receipt_id,
                requested_url=url,
                received_at_utc=received,
                selected_encoding=encoding,
                body=body,
            )
            validate_response(original)
            on_received_html(original)
        try:
            return body.decode(encoding), received, receipt_id
        except (LookupError, UnicodeDecodeError):
            raise ResponseEncodingError("response_encoding") from None


class PairSession(requests.Session):
    def __init__(
        self,
        client: requests.Session,
        deadline: float,
        on_response: Callable[[datetime, int, int], None] | None,
        rate_limiter: HostRateLimiter | None = None,
    ) -> None:
        super().__init__()
        self.client = client
        self.deadline = deadline
        self.on_response = on_response
        self.rate_limiter = rate_limiter
        self.initial_wait_until = time.monotonic() + MIN_REQUEST_INTERVAL_SECONDS
        self.requests = 0
        self.finished_at_utc: datetime | None = None
        self.content_bytes = 0

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeLimit
        return remaining

    def get(self, url: str | bytes, params: Any = None, **kwargs: Any) -> requests.Response:
        if kwargs.get("allow_redirects") is not False:
            raise ValueError("pair redirects are forbidden")
        if self.requests == 0 and self.rate_limiter is None:
            # A fresh two-second wait also covers immediate process/cohort
            # resumes without inferring elapsed time from a persisted clock.
            while (wait := self.initial_wait_until - time.monotonic()) > 0:
                time.sleep(min(wait, self._remaining()))
        self._remaining()
        if self.rate_limiter is not None:
            # Charge actual send time; all buckets start with no restart credit.
            self.rate_limiter.wait(url, deadline=self.deadline)
        remaining = self._remaining()
        kwargs["timeout"] = min(float(kwargs["timeout"]), remaining)
        if params is not None:
            kwargs["params"] = params
        self.requests += 1
        response = self.client.get(url, **kwargs)
        try:
            body = response.content
            finished = datetime.now(UTC)
            self.finished_at_utc = finished
            self.content_bytes += len(body)
            callback = self.on_response
            if callback is not None:
                callback(finished, self.requests, len(body))
            return response
        except BaseException:
            response.close()
            raise
