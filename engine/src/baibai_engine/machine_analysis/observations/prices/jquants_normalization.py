"""Normalize native daily prices independently of storage."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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
        rows.append(
            (
                ticker,
                traded_at,
                to_float(record.get("O")),
                to_float(record.get("H")),
                to_float(record.get("L")),
                to_float(record.get("C")),
                to_float(record.get("Vo")),
                to_float(record.get("Va")),
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
