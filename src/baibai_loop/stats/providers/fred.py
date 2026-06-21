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
    StatsProviderError,
    fetch_text,
    parse_float,
    record_observation,
)


class FredProvider:
    """FRED CSV download (no auth). One column CSV keyed by provider_series_id."""

    name = "fred_csv"

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
        return parse_fred_csv(series, text, start=start, end=end)


def parse_fred_csv(
    series: SeriesDefinition, text: str, *, start: date, end: date
) -> list[ObservationRecord]:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or series.provider_series_id not in reader.fieldnames:
        raise StatsProviderError(f"FRED CSV missing column {series.provider_series_id}")
    observations: list[ObservationRecord] = []
    for row in reader:
        observed_at_raw = row.get("observation_date")
        value_raw = row.get(series.provider_series_id)
        if not observed_at_raw or not value_raw or value_raw == ".":
            continue
        observed_at = date.fromisoformat(observed_at_raw)
        if start <= observed_at <= end:
            observations.append(
                record_observation(series, observed_at=observed_at, value=parse_float(value_raw))
            )
    return observations
