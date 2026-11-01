"""Transport values shared without importing HTTP machinery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
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
    disclosure = (
        parsed.netloc == "www2.jpx.co.jp"
        and re.fullmatch(r"/disc/[0-9A-Z]{4}0/1401\d{14}\.pdf", parsed.path)
        and not parsed.query
        and not parsed.fragment
    )
    if parsed.scheme != "https" or (parsed.netloc != "www.jpx.co.jp" and not disclosure):
        raise JPXAcquisitionError("JPX source must use https://www.jpx.co.jp/")
