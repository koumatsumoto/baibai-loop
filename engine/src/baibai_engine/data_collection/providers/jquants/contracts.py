"""J-Quants native response contracts; no HTTP execution or L2 interpretation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Literal, cast

JQuantsSource = Literal[
    "master",
    "daily_bars",
    "financial_summaries",
    "market_calendar",
    "margin_interest",
    "margin_alerts",
    "short_sale_reports",
]


class JQuantsAcquisitionError(RuntimeError):
    """The source request or its JSON envelope could not be completed."""

    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id


@dataclass(frozen=True, slots=True)
class JQuantsPage:
    """Decompressed HTTP response content, with no credentials or response headers.

    The query identifies this page, including its provider pagination token. The
    body is retained before JSON decoding; it is not an SDK-converted record set.
    """

    source: JQuantsSource
    query: Mapping[str, str]
    received_at: datetime
    body: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "query", MappingProxyType(dict(self.query)))
        if self.received_at.utcoffset() is None:
            raise ValueError("receipt time must be timezone-aware")
        object.__setattr__(self, "received_at", self.received_at.astimezone(UTC))


def _envelope(page: JQuantsPage) -> dict[str, Any]:
    try:
        payload = json.loads(page.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise JQuantsAcquisitionError("J-Quants response is not JSON") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise JQuantsAcquisitionError("J-Quants response lacks a data array")
    if any(not isinstance(row, dict) for row in payload["data"]):
        raise JQuantsAcquisitionError("J-Quants data contains a non-record row")
    return payload


def page_records(page: JQuantsPage) -> list[dict[str, Any]]:
    """Read all source fields without normalizing dates, numbers or missing values."""
    return cast(list[dict[str, Any]], _envelope(page)["data"])
