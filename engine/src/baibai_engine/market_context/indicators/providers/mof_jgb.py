from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from dataclasses import replace
from datetime import date
from urllib.parse import urljoin

from ..definitions import SeriesDefinition
from ..observations import NativeRecords
from .base import (
    MAX_CSV_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    ProviderSpec,
    fetch_bytes,
)

_ERA_OFFSETS = {"S": 1925, "H": 1988, "R": 2018}
_ERA_DATE_RE = re.compile(r"^(?P<era>[SHR])(?P<year>\d+)\.(?P<month>\d+)\.(?P<day>\d+)$")


class MofJgbSource:
    """MOF JGB constant-maturity yield CSV.

    The Ministry of Finance publishes a Shift_JIS CSV with daily constant
    maturity yields from 1974 onward. ``provider_series_id`` is the maturity
    column header, e.g. ``10年``.
    """

    spec = ProviderSpec(name="mof_jgb", all_history_start=date(1974, 1, 1))
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
        records: dict[date, list[dict[str, object]]] = defaultdict(list)
        for url in (series.source_url, _current_month_csv_url(series.source_url)):
            content = fetch_bytes(
                session,
                url,
                params=None,
                max_bytes=MAX_CSV_RESPONSE_BYTES,
                context=context,
            )
            native = parse_mof_jgb_csv(
                replace(series, source_url=url),
                content.decode("cp932"),
                start=start,
                end=end,
            )
            for day, rows in native.items():
                records[day].extend(dict(row) for row in rows)
        return dict(records)


def parse_mof_jgb_csv(
    series: SeriesDefinition, text: str, *, start: date, end: date
) -> NativeRecords:
    reader = csv.reader(io.StringIO(text))
    header: list[str] | None = None
    preamble: list[list[str]] = []
    records: dict[date, list[dict[str, object]]] = defaultdict(list)
    for row in reader:
        if not row:
            continue
        if row[0] == "基準日":
            header = row
            if len(header) != len(set(header)):
                raise IndicatorsProviderError("MOF JGB CSV has duplicate columns")
            if series.provider_series_id not in header:
                raise IndicatorsProviderError(
                    f"MOF JGB CSV missing column {series.provider_series_id}"
                )
            continue
        if header is None:
            preamble.append(row)
            continue
        observed_at = _parse_era_date(row[0])
        if observed_at is None or not start <= observed_at <= end:
            continue
        if len(row) > len(header):
            raise IndicatorsProviderError("MOF JGB CSV row has more fields than its header")
        cells = {column: row[i] if i < len(row) else None for i, column in enumerate(header)}
        records[observed_at].append(
            {
                "row": cells,
                "source_url": series.source_url,
                "preamble": list(preamble),
            }
        )
    if header is None:
        raise IndicatorsProviderError("MOF JGB CSV missing header row")
    return dict(records)


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
