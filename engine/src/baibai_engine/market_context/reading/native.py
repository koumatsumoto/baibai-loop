"""L2 readings over retained native source records."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date
from math import isfinite

from baibai_engine.market_context.indicators.definitions import SeriesDefinition
from baibai_engine.market_context.indicators.observations import ObservationRecord
from baibai_engine.market_context.indicators.read_contracts import native_source_key
from baibai_engine.market_context.indicators.source_snapshots import read_snapshots

from .estat import read_estat_series
from .flows import read_flow_series
from .observations import reading_observation


def read_native_series(
    connection: sqlite3.Connection,
    series: SeriesDefinition,
    *,
    start: date,
    end: date,
    vintage_cutoff: date | None = None,
) -> tuple[ObservationRecord, ...]:
    if series.provider in {"estat", "estat_dashboard"}:
        return read_estat_series(connection, series, start=start, end=end)
    if series.provider == "jquants_flows":
        return read_flow_series(
            connection, series, start=start, end=end, publication_cutoff=vintage_cutoff or end
        )
    observations = []
    for snapshot in read_snapshots(connection, start, end, source_key=native_source_key(series)):
        value = _one_value(series, snapshot.records)
        if value is not None:
            observations.append(
                reading_observation(
                    series,
                    observed_at=snapshot.observed_at,
                    value=value,
                    vintage_at=snapshot.fetched_at,
                )
            )
    return tuple(observations)


def _one_value(series: SeriesDefinition, records: Sequence[Mapping[str, object]]) -> float | None:
    if not records:
        return None
    values = {_record_value(series, row) for row in records}
    # Do not use one valid duplicate to conceal a missing source component.
    if None in values:
        return None
    if len(values) != 1:
        raise ValueError(f"conflicting native inputs for {series.series_id}")
    return values.pop()


def _record_value(series: SeriesDefinition, row: Mapping[str, object]) -> float | None:
    if series.provider == "umich_sca":
        if series.provider_series_id not in row:
            raise ValueError(f"UMich table missing column {series.provider_series_id}")
        raw = row[series.provider_series_id]
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise ValueError("UMich source cell must be text")
        return float(raw) if raw.strip() else None
    if series.provider == "fred_csv":
        if series.provider_series_id not in row:
            raise ValueError(f"FRED CSV missing column {series.provider_series_id}")
        raw = row[series.provider_series_id]
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise ValueError("FRED source cell must be text")
        text = raw.strip()
        return None if text in {"", "."} else float(text)
    if series.provider == "frb_h15":
        if series.unit != "percent":
            raise ValueError(f"FRB H.15 series {series.series_id} has inconsistent unit")
        source_row = row["row"]
        if not isinstance(source_row, Mapping):
            raise ValueError("FRB H.15 source row must be a mapping")
        column = series.provider_series_id
        if column not in source_row:
            raise ValueError(f"FRB H.15 CSV missing column {column}")
        raw = source_row[column]
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise ValueError(f"FRB H.15 source cell must be text: {column}")
        text = raw.strip()
        if text in {"", "ND", "."}:
            return None
        return float(text.replace(",", ""))
    if series.provider == "multpl":
        if series.unit not in {"ratio", "percent"}:
            raise ValueError(f"multpl series {series.series_id} has inconsistent unit")
        return _multpl_cell_value(series, row.get("value_text"))
    if series.provider == "jquants_indices":
        if series.provider_series_id != "topix" or series.unit != "index":
            raise ValueError(f"unsupported native index reading: {series.series_id}")
        return _positive_number(row.get("C"))
    if series.provider == "cftc":
        if series.unit != "contracts":
            raise ValueError(f"CFTC series {series.series_id} has inconsistent unit")
        long = _count(row.get("noncomm_positions_long_all"))
        short = _count(row.get("noncomm_positions_short_all"))
        return None if long is None or short is None else float(long - short)
    if series.provider != "ecb_fx":
        raise ValueError(f"unsupported native reading provider: {series.provider}")
    denominator_column, unit = {
        "EURJPY": (None, "jpy-per-eur"),
        "USDJPY": ("USD", "jpy-per-usd"),
        "AUDJPY": ("AUD", "jpy-per-aud"),
    }[series.provider_series_id]
    if series.unit != unit:
        raise ValueError(f"ECB series {series.series_id} has inconsistent unit")
    numerator = _positive_number(row.get("JPY"))
    denominator = (
        1.0 if denominator_column is None else _positive_number(row.get(denominator_column))
    )
    return None if numerator is None or denominator is None else numerator / denominator


_MULTPL_VALUE = re.compile(r"[†*]*\s*([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(%)?\s*\Z")


def _multpl_cell_value(series: SeriesDefinition, raw: object) -> float | None:
    if isinstance(raw, str) and raw.strip().lower() in {"", "null", "n/a", "na", "nd", "-", "—"}:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"invalid multpl cell for {series.series_id}: {raw!r}")
    match = _MULTPL_VALUE.fullmatch(raw.strip())
    if match is None or (match.group(2) and series.unit != "percent"):
        raise ValueError(f"invalid multpl cell for {series.series_id}: {raw!r}")
    return float(match.group(1).replace(",", ""))


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except (ValueError, OverflowError):
        return None
    return parsed if isfinite(parsed) and parsed > 0 else None
