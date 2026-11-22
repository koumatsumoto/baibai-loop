"""Normalize native daily prices independently of storage."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from math import isfinite
from typing import Any

from baibai_engine.machine_analysis.observations.jquants_decode import normalize_ex_right_type
from baibai_engine.machine_analysis.observations.normalization import (
    NormalizedRows,
    code_quality,
    distinct_source_rows,
    to_float,
    to_str_or_none,
)


def normalize_daily_bars(records: Iterable[Mapping[str, Any]]) -> NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    rejected_count = 0
    excluded_count = 0
    for record in records:
        ticker, code_status = code_quality(record.get("Code"), require_text=True)
        if code_status == "rejected":
            rejected_count += 1
            continue
        if code_status == "excluded":
            excluded_count += 1
            continue
        traded_at = _daily_bar_date(record.get("Date"))
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
                traded_at.isoformat(),
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


def _daily_bar_date(raw: object) -> date | None:
    """Read a session date, including the retained ISO midnight representation."""
    if not isinstance(raw, str):
        return None
    value = raw.removesuffix("T00:00:00")
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _price_values(record: Mapping[str, Any]) -> tuple[float | None, ...] | None:
    fields = ("O", "H", "L", "C", "Vo", "Va")
    values: list[float | None] = []
    observed = False
    try:
        for field in fields:
            raw = record.get(field)
            if raw is None:
                values.append(None)
                continue
            if isinstance(raw, bool) or not isfinite(number := float(raw)):
                return None
            values.append(number)
            observed = True
    except (TypeError, ValueError, OverflowError):
        return None
    # Only six explicit native nulls prove no executions.
    if not observed and any(field not in record for field in fields):
        return None
    return tuple(values)
