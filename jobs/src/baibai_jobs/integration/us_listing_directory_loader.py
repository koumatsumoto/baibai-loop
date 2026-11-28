"""Optional direct receipt of Nasdaq Trader's two public native directories."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from hashlib import sha256
from http import HTTPStatus
from zoneinfo import ZoneInfo

import requests

from baibai_engine.data_collection.providers.nasdaq_symbols import (
    DirectoryFile,
    directory_creation_minute,
    read_us_symbol_directory,
)
from baibai_engine.data_collection.transport.context import (
    HTTP_TIMEOUT_SECONDS,
    MAX_CSV_RESPONSE_BYTES,
    HttpSession,
    IndicatorsProviderError,
    ProviderFetchStoppedError,
)
from baibai_engine.data_collection.transport.http import (
    AcquisitionDeadlineExceeded,
    check_acquisition_deadline,
)
from baibai_jobs.integration.us_listing_refresh import USListingDirectoryReceipt, USListingInputs

_ROOT = "https://www.nasdaqtrader.com/dynamic/SymDir/"
_FILES: tuple[DirectoryFile, DirectoryFile] = ("nasdaqlisted.txt", "otherlisted.txt")
_NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class NasdaqDirectoryCapture:
    file: DirectoryFile
    received_at: datetime
    creation_stamp: str
    body: bytes = field(repr=False)

    @property
    def source_url(self) -> str:
        return _ROOT + self.file

    @property
    def byte_count(self) -> int:
        return len(self.body)

    @property
    def body_sha256(self) -> str:
        return sha256(self.body).hexdigest()


type NasdaqDirectoryPair = tuple[NasdaqDirectoryCapture, NasdaqDirectoryCapture]


def _whole_directory_bytes(session: HttpSession, url: str) -> bytes:
    try:
        check_acquisition_deadline()
        response = session.get(
            url,
            params=None,
            headers={"Accept-Encoding": "identity"},
            timeout=HTTP_TIMEOUT_SECONDS,
            stream=True,
            allow_redirects=False,
        )
        with closing(response):
            if response.status_code != HTTPStatus.OK:
                raise ProviderFetchStoppedError(
                    f"expected HTTP 200 for {url}; got {response.status_code}"
                )
            if response.url != url or "Content-Range" in response.headers:
                raise ProviderFetchStoppedError(
                    f"Nasdaq directory response is not direct and complete: {url}"
                )
            # Content-Length applies to wire bytes; compare only an identity-encoded body.
            if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
                raise ProviderFetchStoppedError(
                    f"Nasdaq directory response is not identity-encoded: {url}"
                )
            declared = response.headers.get("Content-Length")
            try:
                length = (
                    int(declared)
                    if declared is not None and declared.isascii() and declared.isdecimal()
                    else None
                )
            except ValueError as error:
                raise ProviderFetchStoppedError(
                    f"invalid Nasdaq directory Content-Length: {url}"
                ) from error
            if declared is not None and (
                length is None or not 0 < length <= MAX_CSV_RESPONSE_BYTES
            ):
                raise ProviderFetchStoppedError(f"invalid Nasdaq directory Content-Length: {url}")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                check_acquisition_deadline()
                if len(body) + len(chunk) > MAX_CSV_RESPONSE_BYTES:
                    raise ProviderFetchStoppedError(
                        f"Nasdaq directory response exceeds the byte limit: {url}"
                    )
                body.extend(chunk)
            if not body or (length is not None and length != len(body)):
                raise ProviderFetchStoppedError(
                    f"Nasdaq directory response size does not match: {url}"
                )
    except AcquisitionDeadlineExceeded as error:
        raise ProviderFetchStoppedError(str(error)) from error
    except requests.RequestException as error:
        raise IndicatorsProviderError(f"failed to receive Nasdaq directory: {url}") from error
    return bytes(body)


def _today(now: Callable[[], datetime], target: date) -> datetime:
    moment = now()
    if moment.utcoffset() is None or moment.astimezone(_NEW_YORK).date() != target:
        raise ValueError("Nasdaq directories require a current New York receipt date")
    return moment.astimezone(UTC)


def load_verified_nasdaq_directory_inputs(
    session: HttpSession,
    target: date,
    *,
    now: Callable[[], datetime],
    verify_scope: Callable[[date, NasdaqDirectoryPair], bool | None],
) -> USListingInputs:
    """Load only for an authorized owner using a dedicated anonymous HTTP session.

    The refresh owner chooses the date using its independently verified calendar.
    A full HTTP body with a same-date footer still cannot attest that both files
    cover that session's complete listing scopes. No write or fallback lives here.
    """
    if type(target) is not date:
        raise ValueError("Nasdaq directories require a New York session date")
    started_at = _today(now, target)

    def capture(file: DirectoryFile) -> NasdaqDirectoryCapture:
        body = _whole_directory_bytes(session, _ROOT + file)
        received_at = _today(now, target)
        directory = read_us_symbol_directory(body, file=file)
        if directory_creation_minute(directory.creation_stamp).date() != target:
            raise ValueError(f"{file}: native creation date does not match the session")
        return NasdaqDirectoryCapture(file, received_at, directory.creation_stamp, body)

    pair: NasdaqDirectoryPair = (capture(_FILES[0]), capture(_FILES[1]))
    if not started_at <= pair[0].received_at <= pair[1].received_at:
        raise ValueError("Nasdaq directory receipt clock moved backwards")
    if verify_scope(target, pair) is not True:
        raise ValueError("Nasdaq directory session and completeness are unverified")
    return USListingInputs(
        tuple(
            USListingDirectoryReceipt(item.file, target, item.received_at, True, item.body)
            for item in pair
        )
    )
