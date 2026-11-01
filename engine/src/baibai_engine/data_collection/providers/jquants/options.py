"""Acquire complete native daily option chains; do not calculate indicator values."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import date

from baibai_engine.data_collection.provider_contracts import ProviderSpec
from baibai_engine.data_collection.providers.jquants.native import read_native_pages
from baibai_engine.data_collection.transport.context import FetchContext, ProviderFetchStoppedError
from baibai_engine.data_collection.transport.retry import jquants_read_client, read_deadline


class OptionChainSource:
    """One authenticated transport and deadline for an application acquisition pass."""

    spec = ProviderSpec(
        name="jquants_options",
        all_history_rolling_years=10,
        required_env=("JQUANTS_API_KEY",),
    )

    def __init__(self, context: FetchContext) -> None:
        self._deadline = read_deadline("jquants_options", context)
        self._api_key = _read_api_key()
        self._client = jquants_read_client(self._api_key, deadline=self._deadline)

    def fetch(self, day: date) -> list[Mapping[str, object]]:
        self._deadline.check()
        records = read_native_pages(
            self._client,
            "/derivatives/bars/daily/options/225",
            {"date": day.strftime("%Y%m%d")},
            provider="jquants_options",
            api_key=self._api_key,
        )
        _require_requested_day(records, day)
        return records

    def close(self) -> None:
        self._client._request_session().close()


def _read_api_key() -> str:
    name = OptionChainSource.spec.required_env[0]
    token = os.environ.get(name)
    if not token:
        raise ProviderFetchStoppedError(
            f"missing {name}; set it in the environment or .env to fetch jquants_options"
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
