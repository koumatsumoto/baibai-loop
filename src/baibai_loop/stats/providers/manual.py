from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import cast

from baibai_loop.yaml_io import safe_load

from ..db import ObservationRecord
from ..definitions import SeriesDefinition
from .base import (
    FetchContext,
    HttpSession,
    StatsProviderError,
    record_observation,
)

MANUAL_DATA_PATH = Path(__file__).with_name("manual_data.yaml")


class ManualProvider:
    """File-backed provider for series with no clean free API.

    Observations are hand-curated in ``manual_data.yaml`` keyed by
    ``provider_series_id``. No HTTP is performed, so ``session`` and ``context``
    are accepted for protocol conformance but ignored.
    """

    name = "manual"

    def fetch(
        self,
        series: SeriesDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> list[ObservationRecord]:
        raw = safe_load(MANUAL_DATA_PATH.read_text(encoding="utf-8"))
        return parse_manual_entries(series, raw, start=start, end=end)


def parse_manual_entries(
    series: SeriesDefinition, raw_entries: object, *, start: date, end: date
) -> list[ObservationRecord]:
    data = _data_mapping(raw_entries)
    entries = data.get(series.provider_series_id)
    if entries is None:
        raise StatsProviderError(f"manual data missing series {series.provider_series_id}")
    observations: list[ObservationRecord] = []
    for raw_entry in _entry_list(entries):
        entry = _entry_mapping(raw_entry)
        observed_at = _entry_date(entry.get("date"))
        if not start <= observed_at <= end:
            continue
        value = _entry_value(entry.get("value"))
        observations.append(record_observation(series, observed_at=observed_at, value=value))
    return observations


def _data_mapping(raw: object) -> Mapping[str, object]:
    if not isinstance(raw, dict):
        raise StatsProviderError("manual data root must be a mapping")
    return cast(Mapping[str, object], raw)


def _entry_list(raw: object) -> list[object]:
    if not isinstance(raw, list):
        raise StatsProviderError("manual data series entries must be a list")
    return cast(list[object], raw)


def _entry_mapping(raw: object) -> Mapping[str, object]:
    if not isinstance(raw, dict):
        raise StatsProviderError("manual data entry must be a mapping")
    return cast(Mapping[str, object], raw)


def _entry_date(value: object) -> date:
    if isinstance(value, str):
        return date.fromisoformat(value)
    if isinstance(value, date):
        return value
    raise StatsProviderError(f"manual entry 'date' must be a date or ISO string: {value!r}")


def _entry_value(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    raise StatsProviderError(f"manual entry 'value' must be numeric: {value!r}")
