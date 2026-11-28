"""Receive direct, whole Alpaca data or Paper Calendar responses without redirects."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from httpx2 import BaseTransport, Headers, Request, StreamError, TransportError

from baibai_engine.data_collection.providers.alpaca import BARS_URL, BarsResponse
from baibai_engine.data_collection.providers.alpaca_assets import (
    ASSETS_BODY_MAX_BYTES,
    ASSETS_URL,
    AlpacaAssetsReceipt,
)
from baibai_engine.data_collection.providers.alpaca_calendar import (
    CALENDAR_URL,
    AlpacaCalendarReading,
    AlpacaCalendarResponse,
    read_paper_us_calendar,
)
from baibai_engine.data_collection.transport.http import check_acquisition_deadline

BARS_BODY_MAX_BYTES = 128 * 1024
_ENDPOINTS = {
    "bars": (BARS_URL, BARS_BODY_MAX_BYTES),
    "calendar": (CALENDAR_URL, 16 * 1024),
    "assets": (ASSETS_URL, ASSETS_BODY_MAX_BYTES),
}
_TIMEOUT = {"connect": 10.0, "read": 15.0, "write": 10.0, "pool": 10.0}
_CONTENT_HEADERS = frozenset(
    {"content-type", "content-length", "content-encoding", "content-range", "transfer-encoding"}
)


@dataclass(frozen=True, slots=True)
class AlpacaCredentials:
    key: str = field(repr=False)
    secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not all(
            token and token.isascii() and token.isprintable() and " " not in token
            for token in (self.key, self.secret)
        ):
            raise ValueError("Alpaca read credentials are not configured")


@dataclass(frozen=True, slots=True)
class AlpacaHTTPReceipt:
    url: str = field(repr=False)
    response: BarsResponse
    headers: tuple[tuple[str, str], ...]
    retry_after: str | None = field(default=None, repr=False)


class AlpacaTransportReadError(OSError):
    """The transport could not complete a read, not a rejected response contract."""


def _require_whole_body(headers: Headers, size: int) -> None:
    length = headers.get("Content-Length")
    transfer = headers.get("Transfer-Encoding")
    media = headers.get("Content-Type")
    if (
        "Content-Range" in headers
        or headers.get("Content-Encoding", "identity").strip().lower() != "identity"
        or (transfer is not None and (transfer.strip().lower() != "chunked" or length is not None))
        or (
            length is not None
            and (
                not re.fullmatch(r"[0-9]+", length.strip())
                or (length.strip().lstrip("0") or "0") != str(size)
            )
        )
        or (media is not None and media.split(";", 1)[0].strip().lower() != "application/json")
    ):
        raise OSError("Alpaca response is not an exact whole JSON entity")


def receive_alpaca(
    url: str,
    *,
    endpoint: Literal["bars", "calendar", "assets"],
    credentials: AlpacaCredentials,
    transport: BaseTransport,
    clock: Callable[[], datetime],
) -> AlpacaHTTPReceipt:
    """One caller-authorized direct GET, with origin checks before sending credentials.

    A non-200 response only carries the status, not a read or retained error body.
    The caller owns provider-specific authentication, retries, and any persistence.
    """
    base, limit = _ENDPOINTS[endpoint]
    allowed = url == base if endpoint == "assets" else url.startswith(base + "?")
    if not allowed or "#" in url:
        raise OSError("Alpaca request is outside its authorized endpoint")
    request = Request(
        "GET",
        url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "APCA-API-KEY-ID": credentials.key,
            "APCA-API-SECRET-KEY": credentials.secret,
        },
        extensions={"timeout": dict(_TIMEOUT)},
    )
    if str(request.url) != url:
        raise OSError("Alpaca request URL changed before sending")
    check_acquisition_deadline()
    try:
        # The low-level transport does not follow redirects or attach client cookies.
        with closing(transport.handle_request(request)) as response:
            try:
                reported = response.request
            except RuntimeError:
                reported = request
            if reported.method != "GET" or str(reported.url) != url:
                raise OSError("Alpaca response belongs to a different request")
            body = bytearray()
            headers: tuple[tuple[str, str], ...] = ()
            if response.status_code == 200:
                for chunk in response.iter_raw():
                    if len(body) + len(chunk) > limit:
                        raise OSError("Alpaca whole response exceeds its byte bound")
                    body.extend(chunk)
                stamp = clock()
                _require_whole_body(response.headers, len(body))
                headers = tuple(
                    (key, value)
                    for key, value in response.headers.items()
                    if key in _CONTENT_HEADERS
                )
            else:
                stamp = clock()
            return AlpacaHTTPReceipt(
                url,
                BarsResponse(response.status_code, stamp, bytes(body)),
                headers,
                response.headers.get("Retry-After"),
            )
    except (StreamError, TransportError):
        raise AlpacaTransportReadError("Alpaca response could not be fully read") from None
    except ValueError:
        raise OSError("Alpaca response could not be fully read") from None


def receive_paper_calendar(
    start: date,
    end: date,
    *,
    credentials: AlpacaCredentials,
    transport: BaseTransport,
    clock: Callable[[], datetime],
) -> tuple[AlpacaCalendarReading, AlpacaHTTPReceipt | None]:
    """Use the fixed Paper route and preserve its original receipt when reached."""
    wire: AlpacaHTTPReceipt | None = None

    def read_calendar(url: str) -> AlpacaCalendarResponse:
        nonlocal wire
        wire = receive_alpaca(
            url, endpoint="calendar", credentials=credentials, transport=transport, clock=clock
        )
        receipt = wire.response
        return AlpacaCalendarResponse(
            wire.url,
            receipt.status_code,
            receipt.received_at,
            receipt.body,
            receipt.status_code == 200,
        )

    reading = read_paper_us_calendar(start, end, get_authorized=read_calendar)
    return reading, wire


def receive_paper_us_assets(
    *,
    credentials: AlpacaCredentials,
    transport: BaseTransport,
    clock: Callable[[], datetime],
) -> AlpacaAssetsReceipt:
    """Read the active US equity list once from Paper, not Trading or Market Data."""
    wire = receive_alpaca(
        ASSETS_URL, endpoint="assets", credentials=credentials, transport=transport, clock=clock
    )
    response = wire.response
    if response.status_code != 200:
        raise OSError(f"Alpaca Assets HTTP {response.status_code}")
    return AlpacaAssetsReceipt(response.received_at, response.body)
