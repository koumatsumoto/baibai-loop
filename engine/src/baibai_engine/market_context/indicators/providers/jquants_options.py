"""Acquire complete native daily option chains; do not calculate indicator values."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import date
from typing import Any

from baibai_engine.foundation.env import load_project_env

from .base import FetchContext, ProviderFetchStoppedError
from .read_retry import jquants_read_client, read_deadline

_API_KEY_ENV = "JQUANTS_API_KEY"


class OptionChainSource:
    """One authenticated transport and deadline for an application acquisition pass."""

    def __init__(self, context: FetchContext) -> None:
        self._deadline = read_deadline("jquants_options", context)
        self._api_key = _read_api_key()
        self._client = jquants_read_client(self._api_key, deadline=self._deadline)

    def fetch(self, day: date) -> list[Mapping[str, object]]:
        self._deadline.check()
        records = _call(self._client, day, api_key=self._api_key)
        _require_requested_day(records, day)
        return records

    def close(self) -> None:
        self._client._request_session().close()


def _read_api_key() -> str:
    load_project_env()
    token = os.environ.get(_API_KEY_ENV)
    if not token:
        raise ProviderFetchStoppedError(
            f"missing {_API_KEY_ENV}; set it in the environment or .env to fetch jquants_options"
        )
    return token


def _require_requested_day(rows: Sequence[Mapping[str, object]], day: date) -> None:
    """Refuse a chain that answers for a day other than the one asked for.

    The observation is stamped with the requested date, so a session served the
    previous day's chain would carry the previous day's IV readings under today's date with
    nothing to show for it. The payload names the day it belongs to; reading it costs
    one comparison and turns a silent re-dating into a failure.
    """
    answered: set[date] = set()
    for row in rows:
        value = row.get("Date")
        try:
            parsed = date.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            parsed = None
        if parsed is None:
            raise ProviderFetchStoppedError(
                f"jquants_options has a missing or invalid observation date for {day.isoformat()}"
            )
        answered.add(parsed)
    if answered and answered != {day}:
        seen = ", ".join(value.isoformat() for value in sorted(answered)[:3])
        raise ProviderFetchStoppedError(
            f"jquants_options answered for {seen} when {day.isoformat()} was requested"
        )


def _call(client: Any, day: date, *, api_key: str) -> list[Mapping[str, object]]:
    """Read native pages through the pinned SDK's authenticated, retry-mounted GET.

    The public DataFrame method coerces dates and infers field types. The SDK's
    paginator also skips missing/non-list data silently. Keep native records and
    require each page to be an actual response before declaring the day complete.
    """
    url = f"{client.JQUANTS_API_BASE}/derivatives/bars/daily/options/225"
    params = {"date": day.strftime("%Y%m%d")}
    rows: list[Mapping[str, object]] = []
    seen: set[str] = set()
    try:
        while True:
            with closing(client._get(url, params=params)) as response:
                payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ProviderFetchStoppedError("jquants_options page must contain a data list")
            records = payload["data"]
            if not all(isinstance(record, dict) for record in records):
                raise ProviderFetchStoppedError("jquants_options data must contain records")
            rows.extend(records)
            key = payload.get("pagination_key")
            if key is None or key == "":
                return rows
            if not isinstance(key, str) or key in seen:
                raise ProviderFetchStoppedError(
                    "invalid or repeated jquants_options pagination_key"
                )
            seen.add(key)
            params["pagination_key"] = key
    except Exception as exc:
        sanitized = _redact(str(exc), api_key)
        raise ProviderFetchStoppedError(
            f"failed to fetch jquants_options for {day.isoformat()}: "
            f"{type(exc).__name__}: {sanitized}"
        ) from None


def _redact(message: str, secret: str) -> str:
    return message.replace(secret, "***") if secret else message
