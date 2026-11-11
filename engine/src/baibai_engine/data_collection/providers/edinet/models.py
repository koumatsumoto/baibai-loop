"""Types returned by explicit EDINET acquisition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class EDINETProviderError(RuntimeError):
    """Retained EDINET input cannot be interpreted as valid source evidence."""


class EdinetAcquisitionError(RuntimeError):
    """An explicit source request failed without exposing its authenticated URL."""


class EdinetAccessDeniedError(EdinetAcquisitionError):
    """Authentication or access refusal ends this source acquisition pass."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"EDINET request failed with status {status_code}")


class EdinetRateLimitError(EdinetAcquisitionError):
    """The finite source-request budget was exhausted by rate limiting."""


@dataclass(frozen=True, slots=True)
class EdinetResponse:
    received_at: datetime
    body: bytes
