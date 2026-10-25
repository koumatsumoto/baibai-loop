"""Keep native TOPIX OHLC records; the reading layer selects the close."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import date

from baibai_engine.data_collection.economic.provider_specs import JQUANTS_INDICES_SPEC
from baibai_engine.data_collection.economic.records import NativeRecords
from baibai_engine.data_collection.economic.sources import SourceDefinition
from baibai_engine.foundation.env import load_project_env

from .base import FetchContext, HttpSession, ProviderFetchStoppedError
from .jquants_native import read_native_pages
from .read_retry import jquants_read_client, read_deadline

_API_KEY_ENV = JQUANTS_INDICES_SPEC.required_env[0]


def _read_api_key() -> str:
    load_project_env()
    token = os.environ.get(_API_KEY_ENV)
    if not token:
        raise ProviderFetchStoppedError(f"missing {_API_KEY_ENV}")
    return token


class JQuantsIndicesSource:
    def fetch_records(
        self,
        series: SourceDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> NativeRecords:
        if series.provider_series_id != "topix":
            raise ProviderFetchStoppedError(f"unsupported index {series.provider_series_id!r}")
        api_key = _read_api_key()
        client = jquants_read_client(api_key, deadline=read_deadline("jquants_indices", context))
        try:
            rows = read_native_pages(
                client,
                "/indices/bars/daily/topix",
                {"from": start.strftime("%Y%m%d"), "to": end.strftime("%Y%m%d")},
                provider="jquants_indices",
                api_key=api_key,
            )
            return group_index_records(rows, start=start, end=end)
        finally:
            client._request_session().close()


def group_index_records(
    rows: Sequence[Mapping[str, object]],
    *,
    start: date,
    end: date,
) -> NativeRecords:
    grouped: dict[date, list[Mapping[str, object]]] = {}
    for row in rows:
        raw = row.get("Date")
        try:
            observed = date.fromisoformat(raw) if isinstance(raw, str) else None
        except ValueError:
            observed = None
        if observed is None or not start <= observed <= end:
            raise ProviderFetchStoppedError("jquants_indices has an invalid or out-of-window Date")
        grouped.setdefault(observed, []).append(dict(row))
    return grouped
