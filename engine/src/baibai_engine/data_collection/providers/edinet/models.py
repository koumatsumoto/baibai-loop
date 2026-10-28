"""Types returned by explicit EDINET acquisition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class EdinetAcquisitionError(RuntimeError):
    """An explicit source request failed without exposing its authenticated URL."""


class EdinetRateLimitError(EdinetAcquisitionError):
    """The finite source-request budget was exhausted by rate limiting."""


@dataclass(frozen=True, slots=True)
class EdinetResponse:
    received_at: datetime
    body: bytes
