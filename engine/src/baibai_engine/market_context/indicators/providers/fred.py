from __future__ import annotations

import csv
import io
from datetime import date
from urllib.parse import urlsplit

from requests import RequestException, Session

from ..definitions import SeriesDefinition
from ..observations import ObservationRecord
from .base import (
    MAX_CSV_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    ProviderFetchStoppedError,
    ProviderSpec,
    fetch_text,
    parse_float,
    record_observation,
)
from .read_retry import mount_read_retry, read_deadline


class FredProvider:
    """FRED CSV download (no auth). One column CSV keyed by provider_series_id."""

    spec = ProviderSpec(
        name="fred_csv",
        all_history_start=date(1900, 1, 1),
        trim_before_first=True,
    )
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
        try:
            if isinstance(session, Session):
                url = urlsplit(series.source_url)
                mount_read_retry(
                    session,
                    f"{url.scheme}://{url.netloc}/",
                    deadline=read_deadline(self.name, context),
                    max_bytes=MAX_CSV_RESPONSE_BYTES,
                )
            text = fetch_text(
                session,
                series.source_url,
                params=None,
                max_bytes=MAX_CSV_RESPONSE_BYTES,
                context=context,
            )
            return parse_fred_csv(series, text, start=start, end=end)
        except (IndicatorsProviderError, RequestException, ValueError) as exc:
            raise ProviderFetchStoppedError(str(exc)) from None


def parse_fred_csv(
    series: SeriesDefinition, text: str, *, start: date, end: date
) -> list[ObservationRecord]:
    reader = csv.DictReader(io.StringIO(text))
    for column in ("observation_date", series.provider_series_id):
        if column not in (reader.fieldnames or ()):
            raise IndicatorsProviderError(f"FRED CSV missing column {column}")
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
