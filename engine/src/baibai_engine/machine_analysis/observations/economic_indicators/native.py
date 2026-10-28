"""L2 readings over retained native source records."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date
from math import isfinite
from urllib.parse import urljoin

from baibai_engine.data_collection.economic.records import read_snapshots
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import SeriesDefinition
from baibai_engine.machine_analysis.observations.economic_indicators.observations import ObservationRecord

from baibai_engine.machine_analysis.observations.economic_indicators.boj import boj_value
from baibai_engine.machine_analysis.observations.economic_indicators.estat import read_estat_series
from baibai_engine.machine_analysis.observations.economic_indicators.flows import read_flow_series
from baibai_engine.machine_analysis.observations.economic_indicators.validation import reading_observation


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
    for snapshot in read_snapshots(connection, start, end, source_id=series.source_id):
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
    if series.provider == "mof_jgb":
        # The current-month file supersedes overlapping history rows, including
        # explicit missing cells. Both files remain available in the L1 snapshot.
        current_url = urljoin(series.source_url, "../jgbcm.csv")
        current = [row for row in records if row.get("source_url") == current_url]
        records = current or records
    values = {_record_value(series, row) for row in records}
    # Do not use one valid duplicate to conceal a missing source component.
    if None in values:
        return None
    if len(values) != 1:
        raise ValueError(f"conflicting native inputs for {series.series_id}")
    return values.pop()


def _record_value(series: SeriesDefinition, row: Mapping[str, object]) -> float | None:
    if series.provider == "nikkei_indexes":
        if series.reading_key not in {"per", "pbr"} or series.unit != "ratio":
            raise ValueError(f"unsupported Nikkei valuation reading: {series.series_id}")
        raw = row["weighted_text"]
        if not isinstance(raw, str):
            raise ValueError("Nikkei valuation cell must be text")
        text = raw.strip()
        return None if text in {"", "-", "—", "―", "－"} else float(text.replace(",", ""))
    if series.provider == "yahoo":
        metadata = row["metadata"]
        if not isinstance(metadata, Mapping):
            raise ValueError("Yahoo source metadata must be a mapping")
        meta = metadata.get("meta", {})
        if not isinstance(meta, Mapping) or (
            "symbol" in meta and meta["symbol"] != series.reading_key
        ):
            raise ValueError(f"Yahoo source symbol does not match {series.series_id}")
        indicators = row["indicators"]
        if not isinstance(indicators, Mapping):
            raise ValueError("Yahoo source indicators must be a mapping")
        quotes = indicators.get("quote")
        if not isinstance(quotes, list) or not quotes or not isinstance(quotes[0], Mapping):
            raise ValueError("Yahoo source quote missing")
        raw = quotes[0]["close"]
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("Yahoo source close must be numeric or null")
        return float(raw)
    if series.provider == "boj":
        return boj_value(series, row)
    if series.provider == "mof_jgb":
        source_row = row["row"]
        if not isinstance(source_row, Mapping) or series.reading_key not in source_row:
            raise ValueError(f"MOF source missing column {series.reading_key}")
        raw = source_row[series.reading_key]
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise ValueError("MOF source cell must be text")
        text = raw.strip()
        return None if text in {"", "-", "ND", "."} else float(text.replace(",", ""))
    if series.provider == "boj_timeseries":
        metadata = row["series_metadata"]
        expected = series.reading_key.split(":", 1)[1]
        if not isinstance(metadata, Mapping) or metadata.get("SERIES_CODE") != expected:
            raise ValueError(f"BOJ source series does not match {series.series_id}")
        raw = row["value"]
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"BOJ source value must be numeric: {raw!r}")
        return float(raw)
    if series.provider == "umich_sca":
        if series.reading_key not in row:
            raise ValueError(f"UMich table missing column {series.reading_key}")
        raw = row[series.reading_key]
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise ValueError("UMich source cell must be text")
        return float(raw) if raw.strip() else None
    if series.provider == "fred_csv":
        if series.reading_key not in row:
            raise ValueError(f"FRED CSV missing column {series.reading_key}")
        raw = row[series.reading_key]
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
        column = series.reading_key
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
        if series.reading_key != "topix" or series.unit != "index":
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
    }[series.reading_key]
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
