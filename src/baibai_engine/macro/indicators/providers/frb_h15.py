from __future__ import annotations

import csv
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
    parse_optional_float,
    record_observation,
)

# Single H.15 package that carries every Treasury constant-maturity series; one
# download via FetchContext.bytes_cache serves all frb_h15 series in a run.
_H15_PACKAGE_SERIES = "bf17364827e38702b42a58cf8eaa3f78"
# federalreserve.gov serves an HTML block page instead of CSV to non-browser
# User-Agents on datacenter IPs (GitHub Actions); a browser UA is required.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class FrbH15Provider:
    """Federal Reserve H.15 package CSV. Single series or a left-right spread."""

    spec = ProviderSpec(name="frb_h15", all_history_start=date(1962, 1, 1))
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
        params = {
            "rel": "H15",
            "series": _H15_PACKAGE_SERIES,
            "lastObs": "",
            "from": start.strftime("%m/%d/%Y"),
            "to": end.strftime("%m/%d/%Y"),
            "filetype": "csv",
            "label": "include",
            "layout": "seriescolumn",
            "type": "package",
        }
        text = fetch_text(
            session,
            series.source_url,
            params=params,
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            headers={"User-Agent": _USER_AGENT},
            context=context,
        )
        return parse_h15_csv(series, text, start=start, end=end)


def parse_h15_csv(
    series: SeriesDefinition, text: str, *, start: date, end: date
) -> list[ObservationRecord]:
    lines = text.splitlines()
    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith(('"Time Period"', "Time Period,"))
        ),
        None,
    )
    if header_index is None:
        # Surface what the server actually returned (block page, error HTML, or a
        # changed layout) so a remote-only failure is diagnosable from batch logs.
        snippet = " ".join(text.split())[:160]
        raise IndicatorsProviderError(
            f"FRB H.15 CSV missing Time Period header; response starts with: {snippet!r}"
        )
    reader = csv.DictReader(lines[header_index:])
    fieldnames = set(reader.fieldnames or ())
    observations: list[ObservationRecord] = []
    left_id, right_id = _split_provider_series_id(series.provider_series_id)
    if left_id not in fieldnames:
        raise IndicatorsProviderError(f"FRB H.15 CSV missing column {left_id}")
    if right_id is not None and right_id not in fieldnames:
        raise IndicatorsProviderError(f"FRB H.15 CSV missing column {right_id}")
    for row in reader:
        observed_at_raw = row.get("Time Period")
        if not observed_at_raw:
            continue
        observed_at = date.fromisoformat(observed_at_raw)
        if not start <= observed_at <= end:
            continue
        left_value = parse_optional_float(row.get(left_id))
        if left_value is None:
            continue
        if right_id is None:
            value = left_value
        else:
            right_value = parse_optional_float(row.get(right_id))
            if right_value is None:
                continue
            value = (left_value - right_value) * 100.0
        observations.append(record_observation(series, observed_at=observed_at, value=value))
    return observations


def _split_provider_series_id(provider_series_id: str) -> tuple[str, str | None]:
    if "-" not in provider_series_id:
        return provider_series_id, None
    left, right = provider_series_id.split("-", maxsplit=1)
    return left, right
