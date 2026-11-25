"""Capture one public SEC EDGAR JSON response without filing or price interpretation."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Literal
from uuid import UUID

from httpx2 import BaseTransport, Request, Response, StreamError, TransportError

from baibai_engine.primitives.json import canonical_json

_ACCEPT = "application/json"
_ENCODING = "identity"
_MAX_BODY_BYTES = 64 * 1024 * 1024  # This collector's buffer limit, not SEC's coverage.
_TIMEOUT = {"connect": 10.0, "read": 20.0, "write": 10.0, "pool": 10.0}
_CONTACT = re.compile(
    r"[A-Za-z0-9_+%\-]+(?:\.[A-Za-z0-9_+%\-]+)*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
)
_CORE_RECENT_COLUMNS = {"accessionNumber", "form", "acceptanceDateTime"}

type SecEdgarEndpoint = Literal["submissions", "companyfacts"]
type _TransferFailure = Literal[
    "incomplete_transfer",
    "body_limit",
    "unexpected_response_scope",
    "invalid_response",
    "partial_content",
    "http_status",
    "unsupported_content_encoding",
]
type SecEdgarState = _TransferFailure | Literal["received", "invalid_envelope", "issuer_mismatch"]


@dataclass(frozen=True, slots=True)
class SecEdgarQuery:
    """One CIK-specific document; submissions never includes its linked archives."""

    endpoint: SecEdgarEndpoint
    cik: str

    def __post_init__(self) -> None:
        endpoint: object = self.endpoint
        if type(endpoint) is not str or endpoint not in ("submissions", "companyfacts"):
            raise ValueError("unsupported public SEC endpoint")
        candidate: object = self.cik
        if type(candidate) is not str or (cik := _cik(candidate)) is None:
            raise ValueError("SEC request requires a positive ASCII CIK of up to ten digits")
        object.__setattr__(self, "cik", cik)

    @property
    def url(self) -> str:
        path = "submissions" if self.endpoint == "submissions" else "api/xbrl/companyfacts"
        return f"https://data.sec.gov/{path}/CIK{self.cik}.json"

    @property
    def request_id(self) -> str:
        return _identity(["sec-edgar-request-v1", "GET", self.url, _ACCEPT, _ENCODING])


@dataclass(frozen=True, slots=True)
class SecEdgarReceipt:
    """Original received bytes (a prefix on transfer failure), not a filing or public-time proof."""

    request: SecEdgarQuery
    attempt_id: UUID
    received_at: datetime
    status_code: int
    body: bytes = field(repr=False)
    state: SecEdgarState
    older_submissions_advertised: bool | None
    _transfer_failure: _TransferFailure | None = field(repr=False)
    body_sha256: str = field(init=False)
    receipt_id: str = field(init=False)

    def __post_init__(self) -> None:
        digest = sha256(self.body).hexdigest()
        object.__setattr__(self, "body_sha256", digest)
        identity = _identity(
            [
                "sec-edgar-receipt-v1",
                self.request.request_id,
                self.attempt_id.hex,
                self.received_at.isoformat(),
                self.status_code,
                self._transfer_failure,
                digest,
            ]
        )
        object.__setattr__(self, "receipt_id", f"sec-edgar:{identity}")


@dataclass(frozen=True, slots=True)
class SecReportedTicker:
    """One literal ticker the SEC reported for this issuer at receipt, not listing history."""

    symbol: str
    issuer_cik: str
    receipt_id: str
    body_sha256: str
    received_at: datetime
    ticker_pointer: str

    @property
    def identity_ref(self) -> str:
        return f"{self.receipt_id}#{self.ticker_pointer}"


type SecReportedTickerResult = (
    SecReportedTicker
    | Literal[
        "not_yet_received",
        "unusable_submissions_receipt",
        "issuer_mismatch",
        "ticker_list_unverified",
        "ticker_not_reported",
    ]
)


def observe_reported_ticker(
    receipt: SecEdgarReceipt, *, symbol: str, expected_cik: str, cutoff: datetime
) -> SecReportedTickerResult:
    """Require successful, fixed Submissions evidence before joining a quote label to a CIK.

    This proves only the native current association as observed, not the listing
    exchange, share-class title or an effective interval for historical bars.
    Incomplete SEC coverage cannot prove a symbol does not exist.
    """
    expected = SecEdgarQuery("submissions", expected_cik)
    if type(symbol) is not str or not symbol or symbol != symbol.strip():
        raise ValueError("SEC ticker association requires an exact symbol label")
    if cutoff.utcoffset() is None or receipt.received_at.utcoffset() is None:
        raise ValueError("SEC ticker association clocks require a timezone offset")
    received_at = receipt.received_at.astimezone(UTC)
    if received_at > cutoff.astimezone(UTC):
        return "not_yet_received"
    if receipt.request.endpoint != "submissions":
        return "unusable_submissions_receipt"
    if receipt.request.cik != expected.cik or receipt.state == "issuer_mismatch":
        return "issuer_mismatch"
    if (
        receipt.state != "received"
        or receipt._transfer_failure is not None
        or type(receipt.status_code) is not int
        or receipt.status_code != 200
        or type(receipt.body) is not bytes
    ):
        return "unusable_submissions_receipt"

    payload = _read_payload(receipt.body)
    state, _ = _payload_state(expected, payload)
    if state == "issuer_mismatch":
        return state
    if state != "received" or payload is None:
        return "unusable_submissions_receipt"
    tickers = payload.get("tickers")
    if (
        not isinstance(tickers, list)
        or any(
            type(ticker) is not str or not ticker or ticker != ticker.strip() for ticker in tickers
        )
        or len(set(tickers)) != len(tickers)
    ):
        return "ticker_list_unverified"
    if symbol not in tickers:
        return "ticker_not_reported"
    return SecReportedTicker(
        symbol,
        expected.cik,
        receipt.receipt_id,
        receipt.body_sha256,
        received_at,
        f"/tickers/{tickers.index(symbol)}",
    )


def capture_sec_edgar(
    query: SecEdgarQuery,
    *,
    transport: BaseTransport,
    contact_email: str,
    attempt_id: UUID,
    clock: Callable[[], datetime],
) -> SecEdgarReceipt | Literal["transport_error"]:
    """Perform one authorized GET through a caller-owned single-hop streaming transport.

    Caller supplies a fresh attempt ID per actual request and the actual receipt clock.
    SEC fair-access limits must be enforced across live callers, not by this one request.
    A desktop or browser session, filing credentials, retries and redirects are not used.
    """
    if type(query) is not SecEdgarQuery:
        raise ValueError("public SEC capture requires a validated SEC query")
    if (
        type(contact_email) is not str
        or len(contact_email) > 254
        or not _CONTACT.fullmatch(contact_email)
    ):
        raise ValueError("SEC fair access requires a declared contact email")
    if type(attempt_id) is not UUID or attempt_id.int == 0:
        raise ValueError("SEC capture requires a nonzero attempt UUID")
    url = query.url
    request = Request(
        "GET",
        url,
        headers={
            "Accept": _ACCEPT,
            "Accept-Encoding": _ENCODING,
            "User-Agent": f"Baibai Loop {contact_email}",
        },
        extensions={"timeout": dict(_TIMEOUT)},
    )
    # Dispatch at the transport layer: no redirect, cookie, auth or proxy client policies.
    try:
        response = transport.handle_request(request)
    except (OSError, TransportError):
        return "transport_error"
    with closing(response):
        body, stopped = _read_raw(response)
        received_at = clock()
        if type(received_at) is not datetime or received_at.utcoffset() is None:
            raise ValueError("SEC receipt time must have a timezone offset")
        transfer_failure = _transfer_failure(response, request, url, body, stopped)
        state, older = (
            (transfer_failure, None)
            if transfer_failure is not None
            else _payload_state(query, _read_payload(body))
        )
        return SecEdgarReceipt(
            request=query,
            attempt_id=attempt_id,
            received_at=received_at.astimezone(UTC),
            status_code=response.status_code,
            body=body,
            state=state,
            older_submissions_advertised=older,
            _transfer_failure=transfer_failure,
        )


def _read_raw(response: Response) -> tuple[bytes, _TransferFailure | None]:
    # A pre-buffered Response may contain content-decoded bytes. Do not call .content.
    if response.is_stream_consumed or response.is_closed:
        return b"", "invalid_response"
    body = bytearray()
    try:
        for chunk in response.iter_raw():
            space = _MAX_BODY_BYTES - len(body)
            if len(chunk) > space:
                body.extend(chunk[:space])
                # Close while the raw iterator is still held, not from its finalizer.
                with suppress(OSError, StreamError, TransportError):
                    response.close()
                return bytes(body), "body_limit"
            body.extend(chunk)
    except (OSError, StreamError, TransportError):
        return bytes(body), "incomplete_transfer"
    return bytes(body), None


def _transfer_failure(
    response: Response,
    sent: Request,
    url: str,
    body: bytes,
    stopped: _TransferFailure | None,
) -> _TransferFailure | None:
    try:
        reported = response.request
    except RuntimeError:
        reported = sent  # Native BaseTransport returns the single request's response.
    if any(req.method != "GET" or str(req.url) != url for req in (sent, reported)):
        return "unexpected_response_scope"
    if stopped is not None:
        return stopped
    status = response.status_code
    if type(status) is not int or not 100 <= status <= 599:
        return "invalid_response"
    if status == 206:
        return "partial_content"
    if status != 200:
        return "http_status"
    if "Content-Range" in response.headers:
        return "partial_content"
    length = response.headers.get("Content-Length")
    transfer_encoding = response.headers.get("Transfer-Encoding")
    if transfer_encoding is not None and (
        length is not None or transfer_encoding.strip().lower() != "chunked"
    ):
        return "invalid_response"
    if length is not None:
        length = length.strip()
        if not re.fullmatch(r"[0-9]+", length):
            return "invalid_response"
        if (length.lstrip("0") or "0") != str(len(body)):
            return "partial_content"
    if response.headers.get("Content-Encoding", _ENCODING).strip().lower() != _ENCODING:
        return "unsupported_content_encoding"
    media = response.headers.get("Content-Type")
    if media is not None and media.split(";", 1)[0].strip().lower() != _ACCEPT:
        return "invalid_response"
    return None


def _read_payload(body: bytes) -> dict[str, object] | None:
    try:
        payload: object = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_float=Decimal,
            parse_constant=_invalid_constant,
        )
    except (ArithmeticError, RecursionError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _payload_state(
    query: SecEdgarQuery, payload: dict[str, object] | None
) -> tuple[SecEdgarState, bool | None]:
    if payload is None or (cik := _cik(payload.get("cik"))) is None:
        return "invalid_envelope", None
    if cik != query.cik:
        return "issuer_mismatch", None
    if query.endpoint == "companyfacts":
        return ("received" if isinstance(payload.get("facts"), dict) else "invalid_envelope"), None
    filings = payload.get("filings")
    if not isinstance(filings, dict):
        return "invalid_envelope", None
    recent, files = filings.get("recent"), filings.get("files")
    if not isinstance(recent, dict) or not recent.keys() >= _CORE_RECENT_COLUMNS:
        return "invalid_envelope", None
    accessions = recent["accessionNumber"]
    if not isinstance(accessions, list) or any(
        not isinstance(column, list) or len(column) != len(accessions) for column in recent.values()
    ):
        return "invalid_envelope", None
    if not isinstance(files, list) or any(
        not isinstance(item, dict) or type(item.get("name")) is not str or not item["name"].strip()
        for item in files
    ):
        return "invalid_envelope", None
    return "received", bool(files)


def _cik(value: object) -> str | None:
    if type(value) is int and 0 < value <= 9_999_999_999:
        return f"{value:010d}"
    if type(value) is str and re.fullmatch(r"[0-9]{1,10}", value) and int(value) > 0:
        return value.zfill(10)
    return None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate JSON member")
    return value


def _invalid_constant(value: str) -> object:
    raise ValueError("non-JSON number")


def _identity(value: object) -> str:
    return sha256(canonical_json(value).encode()).hexdigest()
