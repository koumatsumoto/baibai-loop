from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date

from ..db import ObservationRecord
from ..definitions import SeriesDefinition
from .base import (
    MAX_CSV_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    ProviderSpec,
    fetch_text,
    parse_float,
    record_observation,
)

# The Nikkei index valuation page renders its PER/PBR history with JavaScript,
# but the table itself is served by the statistics/dataload endpoint one calendar
# month at a time (list=per|pbr, year=YYYY, month=M). We read that endpoint
# directly (no browser); its WAF requires a browser User-Agent plus the AJAX
# headers the page's script sends, and rejects the default client otherwise.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_DATALOAD_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer": "https://indexes.nikkei.co.jp/nkave/archives/data",
    "X-Requested-With": "XMLHttpRequest",
}

# provider_series_id -> the dataload `list` value and the plausible band of the
# weighted-average metric. A column/scale error falls far outside these.
_METRICS: dict[str, tuple[float, float]] = {
    "per": (5.0, 40.0),
    "pbr": (0.5, 4.0),
}

# Each row carries the date, the weighted-average metric (加重平均, the headline
# Nikkei figure) and an index-based figure. We take the weighted average.
_ROW_RE = re.compile(
    r"<td>(\d{4})\.(\d{2})\.(\d{2})</td>\s*"
    r"<!--daily_changing--><td>([\d.]+)</td>\s*"
    r"<!--daily_changing--><td>([\d.]+)</td>"
)


class NikkeiIndexesProvider:
    """Nikkei 225 valuation anchors (PER/PBR) from the official index statistics.

    provider_series_id selects the metric ("per"/"pbr"). The weighted-average
    column is the market-quoted Nikkei figure. Values are range-checked so a
    layout change cannot push a wrong number into the store.
    """

    spec = ProviderSpec(name="nikkei_indexes", all_history_start=date(2010, 1, 1))
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
        metric = series.provider_series_id
        if metric not in _METRICS:
            supported = ", ".join(sorted(_METRICS))
            raise IndicatorsProviderError(
                f"unsupported nikkei_indexes metric {metric!r}; supported: {supported}"
            )
        by_date: dict[date, ObservationRecord] = {}
        for year, month in _months(start, end):
            text = fetch_text(
                session,
                series.source_url,
                params={"list": metric, "type": "1", "year": str(year), "month": str(month)},
                max_bytes=MAX_CSV_RESPONSE_BYTES,
                headers=_DATALOAD_HEADERS,
                context=context,
            )
            for observation in parse_nikkei_valuation(series, text, start=start, end=end):
                by_date[observation.observed_at] = observation
        return [by_date[key] for key in sorted(by_date)]


def parse_nikkei_valuation(
    series: SeriesDefinition,
    text: str,
    *,
    start: date,
    end: date,
) -> list[ObservationRecord]:
    low, high = _METRICS[series.provider_series_id]
    observations: list[ObservationRecord] = []
    for year, month, day, weighted, _index_based in _ROW_RE.findall(text):
        observed_at = date(int(year), int(month), int(day))
        value = parse_float(weighted)
        if not start <= observed_at <= end:
            continue
        if not low <= value <= high:
            raise IndicatorsProviderError(
                f"nikkei_indexes {series.series_id} value {value} outside plausible [{low}, {high}]"
            )
        observations.append(record_observation(series, observed_at=observed_at, value=value))
    return observations


def _months(start: date, end: date) -> Iterator[tuple[int, int]]:
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        month += 1
        if month > 12:
            month = 1
            year += 1
