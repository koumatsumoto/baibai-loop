from __future__ import annotations

import re
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

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

# multpl.com rate-limits the default requests User-Agent; a browser UA is required.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# Each current page exposes one sentence: "Current <name> is <value>[%], a change of ...".
_CURRENT_RE = re.compile(r"Current\s+[^<>]*?\s+is\s+([\d,]+(?:\.\d+)?)")
_HISTORY_DATE_FORMAT = "%b %d, %Y"
_HISTORY_VALUE_RE = re.compile(r"(?:[†*]\s*)?(?P<value>[\d,]+(?:\.\d+)?)%?\Z")
_HISTORY_FLOOR_BY_SLUG = {
    "shiller-pe": date(1871, 2, 1),
    "s-p-500-pe-ratio": date(1871, 1, 1),
    "s-p-500-earnings-yield": date(1871, 1, 1),
}


class MultplProvider:
    """multpl.com valuation scrape (no auth), keyed by the page slug in
    ``provider_series_id`` (e.g. ``shiller-pe``).

    Used for S&P 500 valuation (Shiller CAPE / GAAP PE / earnings yield) which has
    no clean FRED/official feed but anchors the equity-risk-premium lens. Short
    latest-value requests use the current page. Longer requests use the public
    monthly history table, which also carries the current observation.
    """

    spec = ProviderSpec(name="multpl", all_history_start=date(1871, 1, 1))
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
        today = _today_jst()
        if start < date(today.year, today.month, 1):
            history_url = f"{series.source_url.rstrip('/')}/table/by-month"
            text = fetch_text(
                session,
                history_url,
                params=None,
                max_bytes=MAX_CSV_RESPONSE_BYTES,
                headers={"User-Agent": _USER_AGENT},
                context=context,
            )
            return parse_multpl_history(series, text, start=start, end=end)
        text = fetch_text(
            session,
            series.source_url,
            params=None,
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            headers={"User-Agent": _USER_AGENT},
            context=context,
        )
        value = parse_multpl_current(text, series.provider_series_id)
        observed_at = today
        if start <= observed_at <= end:
            return [record_observation(series, observed_at=observed_at, value=value)]
        return []


def parse_multpl_current(text: str, slug: str) -> float:
    match = _CURRENT_RE.search(text)
    if match is None:
        raise IndicatorsProviderError(f"multpl: cannot parse current value for {slug}")
    return float(match.group(1).replace(",", ""))


def parse_multpl_history(
    series: SeriesDefinition,
    text: str,
    *,
    start: date,
    end: date,
) -> list[ObservationRecord]:
    parser = _MultplHistoryParser()
    parser.feed(text)
    parser.close()
    if not parser.found_table:
        raise IndicatorsProviderError(
            f"multpl: historical table missing for {series.provider_series_id}"
        )
    values: dict[date, float] = {}
    ordered_dates: list[date] = []
    for raw_date, raw_value in parser.rows:
        try:
            observed_at = (
                datetime.strptime(raw_date, _HISTORY_DATE_FORMAT).replace(tzinfo=UTC).date()
            )
        except ValueError as exc:
            raise IndicatorsProviderError(
                f"multpl: invalid historical date for {series.provider_series_id}: {raw_date!r}"
            ) from exc
        value_match = _HISTORY_VALUE_RE.fullmatch(raw_value.strip())
        if value_match is None:
            raise IndicatorsProviderError(
                f"multpl: invalid historical value for {series.provider_series_id}: {raw_value!r}"
            )
        value = float(value_match.group("value").replace(",", ""))
        prior_value = values.get(observed_at)
        if prior_value is not None and prior_value != value:
            raise IndicatorsProviderError(
                f"multpl: conflicting values for {series.provider_series_id} on {observed_at}"
            )
        if prior_value is None:
            ordered_dates.append(observed_at)
        values[observed_at] = value

    floor = _HISTORY_FLOOR_BY_SLUG.get(series.provider_series_id)
    if floor is None:
        raise IndicatorsProviderError(
            f"multpl: unknown historical floor for {series.provider_series_id}"
        )
    if start <= floor:
        if not values or min(values) != floor:
            raise IndicatorsProviderError(
                f"multpl: history for {series.provider_series_id} must start at {floor}"
            )
        expected = floor
        required_latest = min(end, _today_jst()).replace(day=1)
        while expected <= required_latest:
            if expected not in values:
                raise IndicatorsProviderError(
                    f"multpl: missing monthly value for {series.provider_series_id}: {expected}"
                )
            expected = (
                date(expected.year + 1, 1, 1)
                if expected.month == 12
                else date(expected.year, expected.month + 1, 1)
            )
    required_latest = min(end, _today_jst()).replace(day=1)
    if required_latest not in values:
        raise IndicatorsProviderError(
            f"multpl: history for {series.provider_series_id} ends before {required_latest}"
        )

    return [
        record_observation(series, observed_at=observed_at, value=values[observed_at])
        for observed_at in ordered_dates
        if start <= observed_at <= end
    ]


class _MultplHistoryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found_table = False
        self.rows: list[tuple[str, str]] = []
        self._in_table = False
        self._in_cell = False
        self._cell_parts: list[str] = []
        self._row_cells: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "table" and attributes.get("id") == "datatable":
            self.found_table = True
            self._in_table = True
        elif self._in_table and tag == "tr":
            self._row_cells = []
        elif self._in_table and self._row_cells is not None and tag == "td":
            self._in_cell = True
            self._cell_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._in_table and tag == "td" and self._in_cell:
            self._in_cell = False
            if self._row_cells is not None:
                self._row_cells.append(" ".join("".join(self._cell_parts).split()))
        elif self._in_table and tag == "tr" and self._row_cells is not None:
            if len(self._row_cells) >= 2:
                self.rows.append((self._row_cells[0], self._row_cells[1]))
            self._row_cells = None
        elif self._in_table and tag == "table":
            self._in_table = False


def _today_jst() -> date:
    return datetime.now(ZoneInfo("Asia/Tokyo")).date()
