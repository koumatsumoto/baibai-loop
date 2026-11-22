"""Retry-After wire deadlines and finite GET retries with an optional job cutoff."""

from __future__ import annotations

import os
import re
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from math import isfinite

from urllib3.exceptions import InvalidHeader
from urllib3.response import BaseHTTPResponse
from urllib3.util import Retry

_BACKOFF_SECONDS = (30, 60, 120, 300, 600)
_DEADLINE_ENV = "BAIBAI_ACQUISITION_DEADLINE"


class AcquisitionDeadlineExceeded(RuntimeError):
    """No new read or retry may start within the remaining job budget."""


def check_acquisition_deadline(waiting: float = 0.0) -> None:
    """An epoch-second cutoff is inherited by CLI subprocesses, never persisted."""
    raw = os.environ.get(_DEADLINE_ENV)
    if raw is None:
        return
    try:
        deadline = float(raw)
    except ValueError:
        raise AcquisitionDeadlineExceeded("invalid acquisition deadline") from None
    if not isfinite(deadline) or time.time() + waiting >= deadline:
        raise AcquisitionDeadlineExceeded("acquisition job deadline exhausted")


def wait_for_read_retry(seconds: float) -> None:
    check_acquisition_deadline(seconds)
    if seconds > 0:
        time.sleep(seconds)
    check_acquisition_deadline()


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
        # second: datetime cannot represent an HTTP leap second.
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
    date = date.astimezone(UTC)
    if leap_second:
        try:
            return max(received, date + timedelta(seconds=1)), True
        except OverflowError:
            return None, True
    return max(received, date), False


def parse_retry_after_deadline(
    value: str | None, received: datetime
) -> tuple[datetime | None, bool]:
    """Parse the wire deadline, separate from any caller's wait budget.

    A true second value means the exact time is unrepresentable. A leap
    second can still carry a conservative boundary at the next whole second.
    """
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


def retry_after_delay(value: str | None, *, fallback: float) -> float:
    """Use the provider's full Retry-After, or the caller's finite backoff."""
    if value is not None:
        try:
            return Retry().parse_retry_after(value)
        except InvalidHeader:
            pass
    return fallback


class ReadRetry(Retry):
    """One GET owns its retries; Retry-After is never shortened to fit the job."""

    def get_backoff_time(self) -> float:
        if not self.history:
            return 0.0
        return float(_BACKOFF_SECONDS[min(len(self.history), 5) - 1])

    def sleep(self, response: BaseHTTPResponse | None = None) -> None:
        delay = self.get_backoff_time()
        if response is not None and self.respect_retry_after_header:
            delay = retry_after_delay(response.headers.get("Retry-After"), fallback=delay)
        wait_for_read_retry(delay)
