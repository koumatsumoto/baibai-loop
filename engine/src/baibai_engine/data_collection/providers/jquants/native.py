"""Read complete native JSON pages without the SDK's DataFrame projection."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from typing import Any

from baibai_engine.data_collection.transport.context import (
    ProviderFetchStoppedError,
    ProviderRateLimitError,
)


def read_native_pages(
    client: Any, endpoint: str, parameters: Mapping[str, str], *, provider: str, api_key: str
) -> list[Mapping[str, object]]:
    """Read native pages through the pinned SDK's authenticated, retry-mounted GET.

    The public DataFrame method coerces dates and infers field types. The SDK's
    paginator also skips missing/non-list data silently. Keep native records and
    require each page to be an actual response before declaring the day complete.
    """
    url = f"{client.JQUANTS_API_BASE}{endpoint}"
    params = dict(parameters)
    rows: list[Mapping[str, object]] = []
    seen: set[str] = set()
    try:
        while True:
            with closing(client._get(url, params=params)) as response:
                payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ProviderFetchStoppedError(f"{provider} page must contain a data list")
            records = payload["data"]
            if not all(isinstance(record, dict) for record in records):
                raise ProviderFetchStoppedError(f"{provider} data must contain records")
            rows.extend(records)
            key = payload.get("pagination_key")
            if key is None or key == "":
                return rows
            if not isinstance(key, str) or key in seen:
                raise ProviderFetchStoppedError(f"invalid or repeated {provider} pagination_key")
            seen.add(key)
            params["pagination_key"] = key
    except Exception as exc:
        sanitized = str(exc).replace(api_key, "***") if api_key else str(exc)
        error_type = (
            ProviderRateLimitError
            if isinstance(exc, ProviderRateLimitError)
            else ProviderFetchStoppedError
        )
        raise error_type(f"failed to fetch {provider}: {type(exc).__name__}: {sanitized}") from None
