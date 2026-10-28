"""Keep investor-type records by publication date, before economic-period selection."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import date

from baibai_engine.data_collection.provider_contracts import JQUANTS_FLOWS_SPEC
from baibai_engine.data_collection.receipts.native_sources.records import NativeRecords
from baibai_engine.data_collection.catalog import SourceDefinition

from baibai_engine.data_collection.transport.context import FetchContext, HttpSession, ProviderFetchStoppedError
from baibai_engine.data_collection.providers.jquants.native import read_native_pages
from baibai_engine.data_collection.transport.retry import jquants_read_client, read_deadline

_API_KEY_ENV = JQUANTS_FLOWS_SPEC.required_env[0]


def _read_api_key() -> str:
    token = os.environ.get(_API_KEY_ENV)
    if not token:
        raise ProviderFetchStoppedError(f"missing {_API_KEY_ENV}")
    return token


class JQuantsFlowsSource:
    def fetch_records(
        self,
        series: SourceDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> NativeRecords:
        api_key = _read_api_key()
        client = jquants_read_client(api_key, deadline=read_deadline("jquants_flows", context))
        try:
            rows = read_native_pages(
                client,
                "/equities/investor-types",
                {
                    "section": "TSEPrime",
                    "from": start.strftime("%Y%m%d"),
                    "to": end.strftime("%Y%m%d"),
                },
                provider="jquants_flows",
                api_key=api_key,
            )
            return group_publications(rows, start=start, end=end)
        finally:
            client._request_session().close()


def group_publications(
    rows: Sequence[Mapping[str, object]],
    *,
    start: date,
    end: date,
) -> NativeRecords:
    grouped: dict[date, list[Mapping[str, object]]] = {}
    for row in rows:
        if row.get("Section") != "TSEPrime":
            raise ProviderFetchStoppedError("jquants_flows answered for another market section")
        raw = row.get("PubDate")
        try:
            published = date.fromisoformat(raw) if isinstance(raw, str) else None
        except ValueError:
            published = None
        if published is None or not start <= published <= end:
            raise ProviderFetchStoppedError("jquants_flows has an invalid or out-of-window PubDate")
        # Keep the complete record, including missing values and economic-period fields.
        grouped.setdefault(published, []).append(dict(row))
    return grouped
