from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from typing import cast

from baibai_engine.data_collection.catalog import SourceDefinition
from baibai_engine.data_collection.provider_contracts import ProviderSpec
from baibai_engine.data_collection.receipts.native_sources.records import NativeRecords
from baibai_engine.data_collection.transport.context import (
    MAX_CSV_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    fetch_text,
)

# CFTC Commitments of Traders, legacy futures-only report (Socrata JSON, no auth).
# provider_series_id is the stable cftc_contract_market_code; the market name
# changes over time (043602 was "10-YEAR U.S. TREASURY NOTES", now "UST 10Y
# NOTE") so the code, not the name, is the identity. The observation is the
# complete source record; net and gross positioning belong to L2.
_MAX_ROWS = 50_000


class CftcSource:
    """Complete CFTC COT native records for one contract code."""

    spec = ProviderSpec(name="cftc", all_history_start=date(2000, 1, 1))
    name = spec.name

    def fetch_records(
        self,
        series: SourceDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> NativeRecords:
        code = series.provider_series_id
        where = (
            f"cftc_contract_market_code='{code}' "
            f"AND report_date_as_yyyy_mm_dd between '{start.isoformat()}T00:00:00' "
            f"and '{end.isoformat()}T23:59:59'"
        )
        text = fetch_text(
            session,
            series.source_url,
            params={
                "$where": where,
                "$order": "report_date_as_yyyy_mm_dd",
                "$limit": str(_MAX_ROWS),
            },
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            context=context,
            retry_transient=True,
        )
        return parse_cftc_records(series, text, start=start, end=end)


def parse_cftc_records(
    series: SourceDefinition, text: str, *, start: date, end: date
) -> NativeRecords:
    try:
        payload: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IndicatorsProviderError(f"CFTC response is not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise IndicatorsProviderError("CFTC response root is not a list")
    if len(payload) >= _MAX_ROWS:
        raise IndicatorsProviderError(
            "CFTC response reached its row limit; completeness is unknown"
        )
    by_date: dict[date, list[dict[str, object]]] = defaultdict(list)
    for entry in payload:
        if not isinstance(entry, dict):
            raise IndicatorsProviderError("CFTC row is not an object")
        row = cast(dict[str, object], entry)
        code = row.get("cftc_contract_market_code")
        if code != series.provider_series_id:
            raise IndicatorsProviderError(
                f"CFTC row code {code!r} does not match requested {series.provider_series_id!r}"
            )
        observed_at = _parse_date(row.get("report_date_as_yyyy_mm_dd"))
        if start <= observed_at <= end:
            by_date[observed_at].append(row)
    return dict(by_date)


def _parse_date(raw: object) -> date:
    if not isinstance(raw, str) or len(raw) < 10:
        raise IndicatorsProviderError(f"CFTC report date is invalid: {raw!r}")
    try:
        return date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise IndicatorsProviderError(f"CFTC report date is invalid: {raw!r}") from exc
