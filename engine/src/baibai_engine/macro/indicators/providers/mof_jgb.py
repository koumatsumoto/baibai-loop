from __future__ import annotations

import csv
import re
from datetime import date
from urllib.parse import urljoin

from ..db import ObservationRecord
from ..definitions import SeriesDefinition
from .base import (
    MAX_CSV_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    ProviderSpec,
    fetch_bytes,
    parse_optional_float,
    record_observation,
)

_ERA_OFFSETS = {"S": 1925, "H": 1988, "R": 2018}
_ERA_DATE_RE = re.compile(r"^(?P<era>[SHR])(?P<year>\d+)\.(?P<month>\d+)\.(?P<day>\d+)$")


class MofJgbProvider:
    """MOF JGB constant-maturity yield CSV.

    The Ministry of Finance publishes a Shift_JIS CSV with daily constant
    maturity yields from 1974 onward. ``provider_series_id`` is the maturity
    column header, e.g. ``10年``.
    """

    spec = ProviderSpec(name="mof_jgb", all_history_start=date(1974, 1, 1))
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
        content = fetch_bytes(
            session,
            series.source_url,
            params=None,
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            context=context,
        )
        observations_by_date = {
            observation.observed_at: observation
            for observation in parse_mof_jgb_csv(
                series, content.decode("cp932"), start=start, end=end
            )
        }
        current_content = fetch_bytes(
            session,
            _current_month_csv_url(series.source_url),
            params=None,
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            context=context,
        )
        observations_by_date.update(
            {
                observation.observed_at: observation
                for observation in parse_mof_jgb_csv(
                    series, current_content.decode("cp932"), start=start, end=end
                )
            }
        )
        return [observations_by_date[key] for key in sorted(observations_by_date)]


def parse_mof_jgb_csv(
    series: SeriesDefinition, text: str, *, start: date, end: date
) -> list[ObservationRecord]:
    reader = csv.reader(text.splitlines())
    header: list[str] | None = None
    observations: list[ObservationRecord] = []
    for row in reader:
        if not row:
            continue
        if row[0] == "基準日":
            header = row
            if series.provider_series_id not in header:
                raise IndicatorsProviderError(
                    f"MOF JGB CSV missing column {series.provider_series_id}"
                )
            continue
        if header is None:
            continue
        observed_at = _parse_era_date(row[0])
        if observed_at is None or not start <= observed_at <= end:
            continue
        value_index = header.index(series.provider_series_id)
        raw_value = row[value_index] if value_index < len(row) else None
        if raw_value == "-":
            continue
        value = parse_optional_float(raw_value)
        if value is None:
            continue
        observations.append(record_observation(series, observed_at=observed_at, value=value))
    if header is None:
        raise IndicatorsProviderError("MOF JGB CSV missing header row")
    return observations


def _parse_era_date(raw: str) -> date | None:
    match = _ERA_DATE_RE.match(raw.strip())
    if match is None:
        return None
    offset = _ERA_OFFSETS[match.group("era")]
    year = offset + int(match.group("year"))
    month = int(match.group("month"))
    day = int(match.group("day"))
    return date(year, month, day)


def _current_month_csv_url(source_url: str) -> str:
    return urljoin(source_url, "../jgbcm.csv")
