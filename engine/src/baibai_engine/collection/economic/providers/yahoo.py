from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import cast

from baibai_engine.collection.economic.provider_specs import ProviderSpec
from baibai_engine.collection.economic.records import NativeRecords
from baibai_engine.collection.economic.sources import SourceDefinition

from .base import (
    MAX_CSV_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    fetch_text,
)

# Yahoo rate-limits the default requests User-Agent; a browser UA is required for 200s.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class YahooChartSource:
    """Yahoo Finance chart JSON (no auth). Daily close keyed by the symbol in
    ``provider_series_id``.

    Used for series with no clean FRED/official feed, e.g. gold front-month
    futures ``GC=F`` (the FRED LBMA gold series was discontinued) and the PHLX
    Semiconductor index ``^SOX`` (proprietary, not on FRED). One symbol per series.
    """

    spec = ProviderSpec(name="yahoo", all_history_start=date(1970, 1, 1))
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
        params = {
            "period1": str(_to_unix(start)),
            "period2": str(_to_unix(end + timedelta(days=1))),
            "interval": "1d",
        }
        text = fetch_text(
            session,
            series.source_url,
            params=params,
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            headers={"User-Agent": _USER_AGENT},
            context=context,
        )
        return parse_yahoo_chart(series, text, start=start, end=end)


def _to_unix(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


def parse_yahoo_chart(
    series: SourceDefinition, text: str, *, start: date, end: date
) -> NativeRecords:
    try:
        payload: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IndicatorsProviderError(f"Yahoo response is not valid JSON: {exc}") from exc
    result = _extract_result(payload, series.provider_series_id)
    timestamps = result.get("timestamp")
    if not isinstance(timestamps, list):
        raise IndicatorsProviderError("Yahoo chart missing timestamp list")
    indicators = _require_mapping(result.get("indicators"), "indicators")
    quote_node = indicators.get("quote")
    if not isinstance(quote_node, list) or not quote_node:
        raise IndicatorsProviderError(f"Yahoo chart missing quote for {series.provider_series_id}")
    first_quote = _require_mapping(quote_node[0], "indicators.quote[0]")
    if "close" not in first_quote:
        raise IndicatorsProviderError("Yahoo chart missing close column")
    # Transpose each source-provided quote/adjusted column, not only the close.
    # Non-time-series result fields (including meta/events) remain unchanged.
    columns: dict[str, list[Mapping[str, object]]] = {}
    for name, groups in indicators.items():
        if not isinstance(groups, list):
            raise IndicatorsProviderError(f"Yahoo indicator group is not a list: {name}")
        columns[name] = []
        for group in groups:
            row = _require_mapping(group, f"indicators.{name}")
            if any(
                not isinstance(values, list) or len(values) != len(timestamps)
                for values in row.values()
            ):
                raise IndicatorsProviderError(
                    f"Yahoo timestamp/indicator lengths differ for {series.provider_series_id}"
                )
            columns[name].append(row)
    metadata = {
        key: value for key, value in result.items() if key not in {"timestamp", "indicators"}
    }
    records: dict[date, list[dict[str, object]]] = defaultdict(list)
    for index, raw_ts in enumerate(timestamps):
        if isinstance(raw_ts, bool) or not isinstance(raw_ts, (int, float)):
            raise IndicatorsProviderError(f"Yahoo invalid timestamp: {raw_ts!r}")
        observed_at = datetime.fromtimestamp(raw_ts, UTC).date()
        if start <= observed_at <= end:
            point = {
                name: [
                    {key: cast(list[object], values)[index] for key, values in group.items()}
                    for group in groups
                ]
                for name, groups in columns.items()
            }
            records[observed_at].append(
                {"timestamp": raw_ts, "metadata": metadata, "indicators": point}
            )
    return dict(records)


def _extract_result(payload: object, symbol: str) -> Mapping[str, object]:
    root = _require_mapping(payload, "root")
    chart = _require_mapping(root.get("chart"), "chart")
    error = chart.get("error")
    if error is not None:
        raise IndicatorsProviderError(f"Yahoo chart error for {symbol}: {error}")
    result = chart.get("result")
    if not isinstance(result, list) or len(result) != 1:
        raise IndicatorsProviderError(f"Yahoo chart must contain one result for {symbol}")
    return _require_mapping(result[0], "chart.result[0]")


def _require_mapping(node: object, label: str) -> Mapping[str, object]:
    if not isinstance(node, dict):
        raise IndicatorsProviderError(f"Yahoo response missing {label} object")
    return cast(Mapping[str, object], node)
