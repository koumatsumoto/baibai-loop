"""Transport values shared without importing HTTP machinery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse


class JPXAcquisitionError(RuntimeError):
    """A JPX source could not be acquired."""


@dataclass(frozen=True, slots=True)
class JPXResponse:
    requested_url: str
    received_at: datetime
    content_type: str
    body: bytes


def validate_jpx_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "www.jpx.co.jp":
        raise JPXAcquisitionError("JPX source must use https://www.jpx.co.jp/")
