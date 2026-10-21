"""Finite GET retry and an optional cutoff for this acquisition job only."""

from __future__ import annotations

import os
import time
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


class ReadRetry(Retry):
    """One GET owns its retries; Retry-After is never shortened to fit the job."""

    def get_backoff_time(self) -> float:
        if not self.history:
            return 0.0
        return float(_BACKOFF_SECONDS[min(len(self.history), 5) - 1])

    def sleep(self, response: BaseHTTPResponse | None = None) -> None:
        delay = self.get_backoff_time()
        if response is not None and self.respect_retry_after_header:
            try:
                retry_after = self.get_retry_after(response)
            except InvalidHeader:
                retry_after = None
            if retry_after is not None:
                delay = retry_after
        wait_for_read_retry(delay)
