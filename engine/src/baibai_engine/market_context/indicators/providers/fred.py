from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date
from urllib.parse import urlsplit

from requests import RequestException, Session

from ..definitions import SeriesDefinition
from ..observations import NativeRecords
from .base import (
    MAX_CSV_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    ProviderFetchStoppedError,
    ProviderSpec,
    fetch_text,
)
from .read_retry import mount_read_retry, read_deadline


class FredSource:
    """Retain FRED CSV cells, including missing markers, before L2 interpretation."""

    spec = ProviderSpec(
        name="fred_csv",
        all_history_start=date(1900, 1, 1),
    )
    name = spec.name

    def fetch_records(
        self,
        series: SeriesDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> NativeRecords:
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
) -> NativeRecords:
    reader = csv.DictReader(io.StringIO(text))
    for column in ("observation_date", series.provider_series_id):
        if column not in (reader.fieldnames or ()):
            raise IndicatorsProviderError(f"FRED CSV missing column {column}")
    columns = reader.fieldnames or []
    if len(columns) != len(set(columns)):
        raise IndicatorsProviderError("FRED CSV has duplicate columns")
    records: dict[date, list[dict[str, object]]] = defaultdict(list)
    for row in reader:
        if None in row:
            raise IndicatorsProviderError("FRED CSV row has more fields than its header")
        observed_at_raw = row.get("observation_date")
        if not observed_at_raw:
            raise IndicatorsProviderError("FRED CSV row has no observation_date")
        observed_at = date.fromisoformat(observed_at_raw)
        if start <= observed_at <= end:
            records[observed_at].append(dict(row))
    return dict(records)
