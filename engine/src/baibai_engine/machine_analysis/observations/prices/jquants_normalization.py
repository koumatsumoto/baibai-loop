"""Normalize native daily prices independently of storage."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from math import isfinite
from typing import Any

from baibai_engine.machine_analysis.observations.jquants_decode import normalize_ex_right_type
from baibai_engine.machine_analysis.observations.normalization import (
    NormalizedRows,
    code_quality,
    date_iso,
    distinct_source_rows,
    to_float,
    to_str_or_none,
)


def normalize_daily_bars(records: Iterable[Mapping[str, Any]]) -> NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    rejected_count = 0
    excluded_count = 0
    for record in records:
        ticker, code_status = code_quality(record.get("Code"))
        if code_status == "rejected":
            rejected_count += 1
            continue
        if code_status == "excluded":
            excluded_count += 1
            continue
        traded_at = date_iso(record.get("Date"))
        if ticker is None or traded_at is None:
            rejected_count += 1
            continue
        prices = _price_values(record)
        if prices is None:
            rejected_count += 1
            continue
        rows.append(
            (
                ticker,
                traded_at,
                *prices,
                to_float(record.get("AdjFactor")),
                to_str_or_none(record.get("UL")),
                to_str_or_none(record.get("LL")),
                normalize_ex_right_type(record.get("ExRT")),
                to_float(record.get("MktCap")),
            )
        )
    return NormalizedRows(
        rows=distinct_source_rows(rows, identity_columns=2, source="daily bars"),
        rejected_count=rejected_count,
        excluded_count=excluded_count,
    )


def _price_values(record: Mapping[str, Any]) -> tuple[float | None, ...] | None:
    fields = ("O", "H", "L", "C", "Vo", "Va")
    try:
        values = tuple(
            None if record.get(field) is None else float(record[field]) for field in fields
        )
    except (TypeError, ValueError, OverflowError):
        return None
    if any(isinstance(record.get(field), bool) for field in fields) or any(
        value is not None and not isfinite(value) for value in values
    ):
        return None
    # Only six explicit native nulls prove no executions. A sparse row can
    # retain a partial observation, but must not manufacture that evidence.
    if all(value is None for value in values) and any(field not in record for field in fields):
        return None
    return values
