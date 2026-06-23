"""J-Quants ingest for the market price/calendar tables: daily bars and calendars."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

from .convert import (
    _code_quality,
    _date_iso,
    _first,
    _NormalizedRows,
    _to_float,
    _to_str_or_none,
)
from .coverage import _delete_date_range, _record_range_source_coverage
from .schema import open_connection


def store_jquants_daily_bars(
    db_path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    requested_start: date,
    requested_end: date,
) -> int:
    conn = open_connection(db_path)
    try:
        records_list = list(records)
        normalized = _bars_rows_with_quality(records_list)
        rows = normalized.rows
        _delete_date_range(conn, "jquants_daily_bars", "traded_at", requested_start, requested_end)
        if rows:
            conn.executemany(
                """
                INSERT OR REPLACE INTO jquants_daily_bars(
                  ticker, traded_at, open, high, low, close, volume, turnover_value,
                  adjustment_open, adjustment_high, adjustment_low, adjustment_close,
                  adjustment_volume, adjustment_factor, upper_limit, lower_limit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        persisted_count = _record_range_source_coverage(
            conn,
            source="jquants_daily_bars",
            operation="get_eq_bars_daily_range",
            table="jquants_daily_bars",
            date_column="traded_at",
            requested_start=requested_start,
            requested_end=requested_end,
            status=normalized.status,
            error=normalized.error,
        )
        conn.commit()
        return persisted_count
    finally:
        conn.close()


def store_jquants_market_calendar(
    db_path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    requested_start: date,
    requested_end: date,
) -> int:
    conn = open_connection(db_path)
    try:
        records_list = list(records)
        normalized = _market_calendar_rows_with_quality(records_list)
        rows = normalized.rows
        _delete_date_range(conn, "jquants_market_calendar", "day", requested_start, requested_end)
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO jquants_market_calendar(day, is_business_day) "
                "VALUES (?, ?)",
                rows,
            )
        persisted_count = _record_range_source_coverage(
            conn,
            source="jquants_market_calendar",
            operation="get_mkt_calendar",
            table="jquants_market_calendar",
            date_column="day",
            requested_start=requested_start,
            requested_end=requested_end,
            status=normalized.status,
            error=normalized.error,
        )
        conn.commit()
        return persisted_count
    finally:
        conn.close()


def _bars_rows_with_quality(records: Iterable[Mapping[str, Any]]) -> _NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    rejected_count = 0
    excluded_count = 0
    for record in records:
        ticker, code_status = _code_quality(_first(record, "Code", "code"))
        if code_status == "rejected":
            rejected_count += 1
            continue
        if code_status == "excluded":
            excluded_count += 1
            continue
        traded_at = _date_iso(_first(record, "Date", "date", "TradedAt", "traded_at"))
        if ticker is None or traded_at is None:
            rejected_count += 1
            continue
        rows.append(
            (
                ticker,
                traded_at,
                _to_float(_first(record, "Open", "open", "O", "o")),
                _to_float(_first(record, "High", "high", "H", "h")),
                _to_float(_first(record, "Low", "low", "L", "l")),
                _to_float(_first(record, "Close", "close", "C", "c")),
                _to_float(_first(record, "Volume", "volume", "Vo", "vo")),
                _to_float(_first(record, "TurnoverValue", "turnover_value", "Va", "va")),
                _to_float(_first(record, "AdjustmentOpen", "adjustment_open", "AdjO", "adj_o")),
                _to_float(_first(record, "AdjustmentHigh", "adjustment_high", "AdjH", "adj_h")),
                _to_float(_first(record, "AdjustmentLow", "adjustment_low", "AdjL", "adj_l")),
                _to_float(_first(record, "AdjustmentClose", "adjustment_close", "AdjC", "adj_c")),
                _to_float(
                    _first(record, "AdjustmentVolume", "adjustment_volume", "AdjVo", "adj_vo")
                ),
                _to_float(_first(record, "AdjustmentFactor", "adjustment_factor", "AdjFactor")),
                _to_str_or_none(_first(record, "UpperLimit", "upper_limit", "UL")),
                _to_str_or_none(_first(record, "LowerLimit", "lower_limit", "LL")),
            )
        )
    return _NormalizedRows(rows=rows, rejected_count=rejected_count, excluded_count=excluded_count)


def _market_calendar_rows_with_quality(records: Iterable[Mapping[str, Any]]) -> _NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    rejected_count = 0
    for record in records:
        day = _date_iso(_first(record, "Date", "date"))
        if day is None:
            rejected_count += 1
            continue
        # Mirror normalize_market_calendar: HolidayDivision "1" (営業日) and "2"
        # (半日営業: 大納会など) both count as business days.
        division = _to_str_or_none(
            _first(record, "HolidayDivision", "holiday_division", "HolDiv", "hol_div")
        )
        is_business_day = 1 if division in {"1", "2"} else 0
        rows.append(
            (
                day,
                is_business_day,
            )
        )
    return _NormalizedRows(rows=rows, rejected_count=rejected_count)
