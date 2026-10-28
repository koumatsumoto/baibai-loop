"""Receive original JPX HTTP bodies; decoding and business rules live elsewhere."""

from __future__ import annotations

from datetime import UTC, datetime

import requests
from requests.adapters import HTTPAdapter

from baibai_engine.data_collection.providers.jpx.models import (
    JPXAcquisitionError,
    JPXResponse,
    validate_jpx_url,
)
from baibai_engine.data_collection.transport.http import ReadRetry, check_acquisition_deadline


class JPXHTTP:
    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session
        self._owns_session = session is None

    def close(self) -> None:
        if self._owns_session and self._session is not None:
            self._session.close()
            self._session = None

    def fetch(self, url: str) -> JPXResponse:
        validate_jpx_url(url)
        check_acquisition_deadline()
        if self._session is None:
            self._session = requests.Session()
            self._session.mount(
                "https://",
                HTTPAdapter(
                    max_retries=ReadRetry(
                        total=5,
                        allowed_methods=frozenset({"GET"}),
                        status_forcelist=(429, 500, 502, 503, 504),
                    )
                ),
            )
        try:
            response = self._session.get(url, timeout=30)
            try:
                if response.status_code != 200:
                    raise JPXAcquisitionError(
                        f"JPX source request failed: HTTP {response.status_code}"
                    )
                body = response.content
                captured_at = datetime.now(UTC)
                return JPXResponse(
                    requested_url=url,
                    received_at=captured_at,
                    content_type=response.headers.get("content-type", ""),
                    body=body,
                )
            finally:
                response.close()
        except requests.RequestException:
            raise JPXAcquisitionError("JPX source request failed") from None
