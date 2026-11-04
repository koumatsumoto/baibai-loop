"""J-Quants native response contracts; no HTTP execution or L2 interpretation."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
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


class JQuantsRateLimitError(JQuantsAcquisitionError):
    """The final HTTP 429 stops further J-Quants acquisition in this pass."""


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


def page_envelope(page: JQuantsPage) -> dict[str, Any]:
    """Decode and validate native rows while preserving pagination metadata."""
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
    return cast(list[dict[str, Any]], page_envelope(page)["data"])


@dataclass(frozen=True, slots=True)
class JQuantsRequest:
    source: JQuantsSource
    params: Mapping[str, str]
    pages: tuple[JQuantsPage, ...]

    @property
    def received_at(self) -> datetime:
        return self.pages[-1].received_at

    def records(self) -> list[dict[str, Any]]:
        return [record for page in self.pages for record in page_records(page)]


def complete_request(pages: Sequence[JQuantsPage]) -> JQuantsRequest:
    """Validate the same native pagination chain in a bounded dependency handoff."""
    if not pages:
        raise JQuantsAcquisitionError("J-Quants request has no pages")
    source = pages[0].source
    base = dict(pages[0].query)
    if "pagination_key" in base:
        raise JQuantsAcquisitionError("J-Quants request starts at a continuation page")
    expected_query = dict(base)
    seen: set[str] = set()
    for index, page in enumerate(pages):
        if page.source != source or dict(page.query) != expected_query:
            raise JQuantsAcquisitionError("J-Quants request pagination identity differs")
        payload = page_envelope(page)
        cursor = payload.get("pagination_key")
        if cursor is None or cursor == "":
            if index != len(pages) - 1:
                raise JQuantsAcquisitionError("J-Quants request continues after its final page")
            return JQuantsRequest(source, pages[0].query, tuple(pages))
        if not isinstance(cursor, str) or cursor in seen:
            raise JQuantsAcquisitionError("invalid or repeated J-Quants pagination key")
        seen.add(cursor)
        expected_query = {**base, "pagination_key": cursor}
    raise JQuantsAcquisitionError("J-Quants request is incomplete")


@dataclass(frozen=True, slots=True)
class RetainedRequest:
    request_id: str
    request: JQuantsRequest | None
    error: str | None
    first_query: dict[str, str] | None = None
    last_received_at: datetime | None = None
    page_fingerprints: tuple[tuple[int, str], ...] = ()
