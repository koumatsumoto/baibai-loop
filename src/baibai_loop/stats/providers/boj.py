from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from datetime import date

from ..db import ObservationRecord
from ..definitions import SeriesDefinition
from .base import (
    MAX_CSV_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    StatsProviderError,
    fetch_bytes,
    parse_optional_float,
    record_observation,
)

# BOJ stat-search flags gaps with these tokens; each means "no observation".
_BOJ_MISSING_VALUES = frozenset({"", "NA", "ND", "*", "."})


class BojProvider:
    """Bank of Japan time-series stat-search CSV (no auth).

    The download carries Japanese description rows and is encoded Shift-JIS
    (cp932), so we fetch raw bytes and decode utf-8-sig first, falling back to
    cp932 when that fails. Values are keyed off provider_series_id: the column
    that holds the data code in the header row is the same column that holds
    values in every date row, so column 0 is always the time period.
    """

    name = "boj"

    def fetch(
        self,
        series: SeriesDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> list[ObservationRecord]:
        content = fetch_bytes(
            session,
            series.source_url,
            params=None,
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            context=context,
        )
        return parse_boj_csv(series, _decode_boj_csv(content), start=start, end=end)


def parse_boj_csv(
    series: SeriesDefinition, text: str, *, start: date, end: date
) -> list[ObservationRecord]:
    rows = [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(text))]
    value_col = _find_value_column(rows, series.provider_series_id)
    observations: list[ObservationRecord] = []
    saw_date_row = False
    for row in rows:
        if not row:
            continue
        observed_at = _parse_boj_date(row[0])
        if observed_at is None:
            continue
        saw_date_row = True
        if value_col >= len(row):
            continue
        raw_value = row[value_col]
        if raw_value in _BOJ_MISSING_VALUES:
            continue
        value = parse_optional_float(raw_value)
        if value is None:
            continue
        if start <= observed_at <= end:
            observations.append(record_observation(series, observed_at=observed_at, value=value))
    if not saw_date_row:
        raise StatsProviderError("BOJ CSV has no parseable date rows")
    return observations


def _find_value_column(rows: Sequence[Sequence[str]], provider_series_id: str) -> int:
    # Column 0 holds the time period, so the data code lives in a value column.
    for row in rows:
        for index, cell in enumerate(row):
            if index >= 1 and cell == provider_series_id:
                return index
    raise StatsProviderError(f"BOJ CSV missing series code {provider_series_id}")


def _parse_boj_date(raw: str) -> date | None:
    parts = raw.strip().replace("/", "-").split("-")
    if len(parts) not in {2, 3}:
        return None
    year_text = parts[0]
    if len(year_text) != 4 or not year_text.isdigit():
        return None
    try:
        year = int(year_text)
        month = int(parts[1])
        day = int(parts[2]) if len(parts) == 3 else 1
    except ValueError:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _decode_boj_csv(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("cp932")
