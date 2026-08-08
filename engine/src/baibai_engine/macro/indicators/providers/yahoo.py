from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import cast
from urllib.parse import quote

from ..db import ObservationRecord
from ..definitions import SeriesDefinition
from .base import (
    MAX_CSV_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    ProviderSpec,
    fetch_text,
    record_observation,
)

_CHART_BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
# Yahoo rate-limits the default requests User-Agent; a browser UA is required for 200s.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class YahooChartProvider:
    """Yahoo Finance chart JSON (no auth). Daily close keyed by the symbol in
    ``provider_series_id``.

    Used for series with no clean FRED/official feed, e.g. gold front-month
    futures ``GC=F`` (the FRED LBMA gold series was discontinued) and the PHLX
    Semiconductor index ``^SOX`` (proprietary, not on FRED). One symbol per series.
    """

    spec = ProviderSpec(name="yahoo", all_history_start=date(1970, 1, 1))
    name = spec.name

    def fetch(
        self,
        series: SeriesDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> list[ObservationRecord]:
        symbol = quote(series.provider_series_id, safe="=")
        url = f"{_CHART_BASE_URL}/{symbol}"
        params = {
            "period1": str(_to_unix(start)),
            "period2": str(_to_unix(end + timedelta(days=1))),
            "interval": "1d",
        }
        text = fetch_text(
            session,
            url,
            params=params,
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            headers={"User-Agent": _USER_AGENT},
            context=context,
        )
        return parse_yahoo_chart(series, text, start=start, end=end)


def _to_unix(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


def parse_yahoo_chart(
    series: SeriesDefinition, text: str, *, start: date, end: date
) -> list[ObservationRecord]:
    try:
        payload: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IndicatorsProviderError(f"Yahoo response is not valid JSON: {exc}") from exc
    timestamps, closes = _extract_series(payload, series.provider_series_id)
    observations: list[ObservationRecord] = []
    for raw_ts, raw_close in zip(timestamps, closes, strict=False):
        if not isinstance(raw_ts, (int, float)) or not isinstance(raw_close, (int, float)):
            continue
        observed_at = datetime.fromtimestamp(raw_ts, UTC).date()
        if start <= observed_at <= end:
            observations.append(
                record_observation(series, observed_at=observed_at, value=float(raw_close))
            )
    return observations


def _extract_series(payload: object, symbol: str) -> tuple[Sequence[object], Sequence[object]]:
    if not isinstance(payload, dict):
        raise IndicatorsProviderError("Yahoo response is not a JSON object")
    chart = _require_mapping(cast(Mapping[str, object], payload).get("chart"), "chart")
    error = chart.get("error")
    if error is not None:
        raise IndicatorsProviderError(f"Yahoo chart error for {symbol}: {error}")
    result = chart.get("result")
    if not isinstance(result, list) or not result:
        raise IndicatorsProviderError(f"Yahoo chart has no result for {symbol}")
    first = _require_mapping(result[0], "chart.result[0]")
    indicators = _require_mapping(first.get("indicators"), "indicators")
    quote_node = indicators.get("quote")
    if not isinstance(quote_node, list) or not quote_node:
        raise IndicatorsProviderError(f"Yahoo chart missing quote for {symbol}")
    quote0 = _require_mapping(quote_node[0], "indicators.quote[0]")
    timestamps = first.get("timestamp")
    closes = quote0.get("close")
    if not isinstance(timestamps, list) or not isinstance(closes, list):
        raise IndicatorsProviderError(f"Yahoo chart missing timestamp/close for {symbol}")
    return cast(Sequence[object], timestamps), cast(Sequence[object], closes)


def _require_mapping(node: object, label: str) -> Mapping[str, object]:
    if not isinstance(node, dict):
        raise IndicatorsProviderError(f"Yahoo response missing {label} object")
    return cast(Mapping[str, object], node)
