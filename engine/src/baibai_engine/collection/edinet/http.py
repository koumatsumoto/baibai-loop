"""EDINET HTTP reception only; no market projection or implicit cache reads."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from math import isfinite
from urllib.parse import urlencode

import requests

from baibai_engine.collection.edinet.models import (
    EdinetAcquisitionError,
    EdinetRateLimitError,
    EdinetResponse,
)

EDINET_API_BASE = "https://api.edinet-fsa.go.jp/api/v2"
_DOC_ID = re.compile(r"S[0-9A-Z]{3,32}\Z")


class EDINETHTTP:
    def __init__(self, api_key: str, *, session: requests.Session | None = None) -> None:
        if not api_key:
            raise ValueError("EDINET API key is required for acquisition")
        self._api_key = api_key
        self._session = session if session is not None else requests.Session()
        self._owns_session = session is None

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def fetch_list(self, on_date: date) -> EdinetResponse:
        return self._receive("documents.json", {"date": on_date.isoformat(), "type": "2"}, 30)

    def fetch_archive(self, doc_id: str, source_type: int) -> EdinetResponse:
        if not _DOC_ID.fullmatch(doc_id):
            raise ValueError("EDINET acquisition requires a normalized document id")
        if source_type not in (1, 5) or isinstance(source_type, bool):
            raise ValueError("EDINET archive type must be XBRL (1) or CSV (5)")
        return self._receive(f"documents/{doc_id}", {"type": str(source_type)}, 60)

    def _receive(self, path: str, params: Mapping[str, str], timeout: int) -> EdinetResponse:
        query = urlencode({**params, "Subscription-Key": self._api_key})
        url = f"{EDINET_API_BASE}/{path}?{query}"
        for attempt in range(6):
            response: requests.Response | None = None
            network_error: str | None = None
            try:
                response = self._session.get(url, timeout=timeout, allow_redirects=False)
                body = bytes(response.content)
                received_at = datetime.now(UTC)
            except requests.RequestException as error:
                # Keep the original exception (which may contain the key URL) out
                # of the raised error's cause and context, including body-read errors.
                network_error = type(error).__name__
            finally:
                if response is not None:
                    response.close()
            if network_error is not None:
                if attempt == 5:
                    raise EdinetAcquisitionError(f"EDINET transport failed: {network_error}")
                time.sleep(3 * (2**attempt))
                continue
            assert response is not None
            rate_limited = response.status_code == 429 or (
                response.status_code == 200 and _body_status(body) == "429"
            )
            if response.status_code == 200 and not rate_limited:
                return EdinetResponse(received_at=received_at, body=body)
            if not rate_limited and response.status_code not in {500, 502, 503, 504}:
                raise EdinetAcquisitionError(
                    f"EDINET request failed with status {response.status_code}"
                )
            wait = _retry_after(response.headers.get("Retry-After", ""), received_at)
            if not rate_limited or wait is None:
                wait = float(3 * (2**attempt))
            if attempt == 5:
                if rate_limited:
                    raise EdinetRateLimitError("EDINET request rate limited after retries")
                raise EdinetAcquisitionError("EDINET request failed after retries")
            time.sleep(wait)
        raise AssertionError("unreachable EDINET retry state")


def _body_status(body: bytes) -> str | None:
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(value, dict):
        return None
    status = value.get("StatusCode") or value.get("statusCode") or value.get("status")
    return str(status) if status not in (None, "") else None


def _retry_after(value: str, received_at: datetime) -> float | None:
    text = value.strip()
    if text.isascii() and text.isdigit():
        seconds = float(text)
        return seconds if isfinite(seconds) else None
    try:
        deadline = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if deadline.tzinfo is None:
        return None
    return max(0.0, (deadline - received_at).total_seconds())
