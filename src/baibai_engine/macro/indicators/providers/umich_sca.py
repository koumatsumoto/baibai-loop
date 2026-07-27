from __future__ import annotations

import csv
import io
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

# The Surveys of Consumers publish their index tables as CSV keyed by a spelled-out
# month name and a separate year column, so the observation date is assembled from
# the pair. The names are English in the source, so they are fixed here instead of
# being read from the runtime locale.
_MONTH_NUMBERS = {
    "January": 1,
    "February": 2,
    "March": 3,
    "April": 4,
    "May": 5,
    "June": 6,
    "July": 7,
    "August": 8,
    "September": 9,
    "October": 10,
    "November": 11,
    "December": 12,
}


class UmichScaProvider:
    """University of Michigan Surveys of Consumers index table (no auth).

    ``provider_series_id`` names the value column (``ICS_ALL`` is the Index of
    Consumer Sentiment) and ``source_url`` names the table, so another table from
    the same publisher is a registry entry rather than a code change.

    Reading the publisher directly keeps the series on the publisher's own release
    calendar. A relay adds its own ingestion lag, and a relay that stops is
    indistinguishable in the store from a survey that stopped.

    The table carries one row per published month, so a month appears once its
    reading is final; the monthly history reaches back to 1952 and is quarterly
    before 1978, which leaves the early months sparse rather than missing.
    """

    spec = ProviderSpec(name="umich_sca", all_history_start=date(1952, 11, 1))
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
        text = fetch_text(
            session,
            series.source_url,
            params=None,
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            context=context,
        )
        return parse_umich_table(series, text, start=start, end=end)


def parse_umich_table(
    series: SeriesDefinition, text: str, *, start: date, end: date
) -> list[ObservationRecord]:
    """Read the requested column of a Surveys of Consumers table.

    A row whose month or year cannot be read is rejected rather than skipped: the
    table has one shape, so an unreadable row means the source changed and the
    fetch must fail instead of quietly returning a shorter history.
    """

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or series.provider_series_id not in reader.fieldnames:
        raise IndicatorsProviderError(f"UMich table missing column {series.provider_series_id}")
    observations: list[ObservationRecord] = []
    for row in reader:
        value_raw = (row.get(series.provider_series_id) or "").strip()
        if not value_raw:
            continue
        observed_at = _observed_at(row)
        if start <= observed_at <= end:
            observations.append(
                record_observation(series, observed_at=observed_at, value=parse_float(value_raw))
            )
    return observations


def _observed_at(row: dict[str, str | None]) -> date:
    month = (row.get("Month") or "").strip()
    year = (row.get("YYYY") or "").strip()
    if month not in _MONTH_NUMBERS or not year.isdigit():
        raise IndicatorsProviderError(
            f"UMich table row is not a month of a year: {month!r} {year!r}"
        )
    return date(int(year), _MONTH_NUMBERS[month], 1)
