"""J-Quants ingest: bars, fin summaries, master, calendars."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

from baibai_loop.screening.providers.jquants import (
    normalize_sector_name,
)

from .convert import (
    _code_quality,
    _date_iso,
    _first,
    _is_common_stock_flag,
    _NormalizedRows,
    _to_float,
    _to_str_or_none,
)
from .schema import open_connection
from .source_coverage import (
    _delete_date_range,
    _delete_source_coverage,
    _record_range_source_coverage,
    _record_source_coverage,
    _table_row_count,
)


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


def store_jquants_fin_summaries(
    db_path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    requested_start: date,
    requested_end: date,
) -> int:
    conn = open_connection(db_path)
    try:
        records_list = list(records)
        normalized = _fin_summary_rows_with_quality(records_list)
        rows = normalized.rows
        _delete_date_range(
            conn, "jquants_fin_summaries", "disclosed_at", requested_start, requested_end
        )
        if rows:
            conn.executemany(
                """
                INSERT OR REPLACE INTO jquants_fin_summaries(
                  ticker, disclosed_at, forecast_eps, eps_ttm, bps, shares_outstanding,
                  sales, cfo, cash_eq, total_assets, equity, operating_profit, ordinary_profit,
                  profit, fiscal_period, fiscal_year_end, period_start, period_end
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        persisted_count = _record_range_source_coverage(
            conn,
            source="jquants_fin_summaries",
            operation="get_fin_summary_range",
            table="jquants_fin_summaries",
            date_column="disclosed_at",
            requested_start=requested_start,
            requested_end=requested_end,
            status=normalized.status,
            error=normalized.error,
        )
        conn.commit()
        return persisted_count
    finally:
        conn.close()


def store_jquants_master(db_path: Path, records: Iterable[Mapping[str, Any]]) -> int:
    conn = open_connection(db_path)
    try:
        records_list = list(records)
        normalized = _master_rows_with_quality(records_list)
        rows = normalized.rows
        conn.execute("DELETE FROM jquants_master_snapshots")
        _delete_source_coverage(conn, "jquants_master_snapshots")
        if rows:
            conn.executemany(
                """
                INSERT OR REPLACE INTO jquants_master_snapshots(
                  snapshot_date, ticker, name, market, sector_33, is_common_stock
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        persisted_count = _table_row_count(conn, "jquants_master_snapshots")
        dates = sorted({row[0] for row in rows if row[0] != "unknown"})
        _record_source_coverage(
            conn,
            source="jquants_master_snapshots",
            operation="get_eq_master",
            coverage_key="latest",
            coverage_start=dates[0] if dates else None,
            coverage_end=dates[-1] if dates else None,
            requested_start=None,
            requested_end=None,
            params={},
            record_count=persisted_count,
            raw_record_count=len(records_list),
            skipped_record_count=normalized.skipped_count,
            rejected_record_count=normalized.rejected_count,
            excluded_record_count=normalized.excluded_count,
            status=normalized.status,
            error=normalized.error,
        )
        conn.commit()
        return persisted_count
    finally:
        conn.close()


def store_jquants_earnings_calendar(
    db_path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    requested_start: date | None = None,
    requested_end: date | None = None,
) -> int:
    conn = open_connection(db_path)
    try:
        records_list = list(records)
        normalized = _earnings_calendar_rows_with_quality(records_list)
        rows = normalized.rows
        conn.execute("DELETE FROM jquants_earnings_calendar")
        _delete_source_coverage(conn, "jquants_earnings_calendar")
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO jquants_earnings_calendar("
                "announcement_date, ticker"
                ") VALUES (?, ?)",
                rows,
            )
        persisted_count = _table_row_count(conn, "jquants_earnings_calendar")
        dates = sorted({row[0] for row in rows})
        coverage_start = (
            requested_start.isoformat() if requested_start else dates[0] if dates else None
        )
        coverage_end = requested_end.isoformat() if requested_end else dates[-1] if dates else None
        params = {
            key: value.isoformat()
            for key, value in (("start_dt", requested_start), ("end_dt", requested_end))
            if value is not None
        }
        _record_source_coverage(
            conn,
            source="jquants_earnings_calendar",
            operation="get_eq_earnings_cal",
            coverage_key="whole-list",
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            requested_start=requested_start.isoformat() if requested_start else None,
            requested_end=requested_end.isoformat() if requested_end else None,
            params=params,
            record_count=persisted_count,
            raw_record_count=len(records_list),
            skipped_record_count=normalized.skipped_count,
            rejected_record_count=normalized.rejected_count,
            excluded_record_count=normalized.excluded_count,
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


def _earnings_calendar_rows_with_quality(records: Iterable[Mapping[str, Any]]) -> _NormalizedRows:
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
        announcement_date = _date_iso(
            _first(record, "Date", "date", "AnnouncementDate", "announcement_date")
        )
        if ticker is None or announcement_date is None:
            rejected_count += 1
            continue
        rows.append(
            (
                announcement_date,
                ticker,
            )
        )
    return _NormalizedRows(rows=rows, rejected_count=rejected_count, excluded_count=excluded_count)


def _earnings_calendar_rows(records: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    return _earnings_calendar_rows_with_quality(records).rows


def _market_calendar_rows_with_quality(records: Iterable[Mapping[str, Any]]) -> _NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    rejected_count = 0
    for record in records:
        day = _date_iso(_first(record, "Date", "date"))
        if day is None:
            rejected_count += 1
            continue
        # Mirror JQuantsProvider: HolidayDivision "1" (営業日) and "2"
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


def _market_calendar_rows(records: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    return _market_calendar_rows_with_quality(records).rows


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


def _fin_summary_rows_with_quality(records: Iterable[Mapping[str, Any]]) -> _NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    rejected_count = 0
    excluded_count = 0
    for record in records:
        ticker, quality = _code_quality(_first(record, "Code", "code"))
        if quality == "rejected":
            rejected_count += 1
            continue
        if quality == "excluded":
            excluded_count += 1
            continue
        disclosed_at = _date_iso(
            _first(record, "DisclosedDate", "disclosed_at", "DiscDate", "disc_date", "Date")
        )
        if ticker is None or disclosed_at is None:
            rejected_count += 1
            continue
        rows.append(
            (
                ticker,
                disclosed_at,
                _to_float(_first(record, "ForecastEPS", "forecast_eps", "FEPS")),
                _to_float(_first(record, "EpsTtm", "eps_ttm", "EPS", "eps")),
                _to_float(_first(record, "BPS", "bps")),
                _to_float(
                    _first(
                        record,
                        "SharesOutstanding",
                        "shares_outstanding",
                        "IssuedShareEquityQuote",
                        "ShOutFY",
                        "AvgSh",
                    )
                ),
                _to_float(_first(record, "NetSales", "net_sales", "Sales", "sales")),
                _to_float(
                    _first(
                        record,
                        "CashFlowsFromOperatingActivities",
                        "cash_flows_from_operating_activities",
                        "OperatingCashFlow",
                        "operating_cash_flow",
                        "CFO",
                        "cfo",
                    )
                ),
                _to_float(
                    _first(
                        record,
                        "CashAndEquivalents",
                        "cash_and_equivalents",
                        "CashEq",
                        "cash_eq",
                    )
                ),
                _to_float(_first(record, "TotalAssets", "total_assets", "TA", "ta")),
                _to_float(_first(record, "Equity", "equity", "Eq", "eq")),
                _to_float(_first(record, "OperatingProfit", "operating_profit", "OP")),
                _to_float(_first(record, "OrdinaryProfit", "ordinary_profit", "OdP")),
                _to_float(_first(record, "Profit", "profit", "NP")),
                _to_str_or_none(
                    _first(record, "TypeOfCurrentPeriod", "type_of_current_period", "CurPerType")
                ),
                _date_iso(
                    _first(
                        record,
                        "CurrentFiscalYearEndDate",
                        "current_fiscal_year_end_date",
                        "CurFYEn",
                    )
                ),
                _date_iso(
                    _first(
                        record, "CurrentPeriodStartDate", "current_period_start_date", "CurPerSt"
                    )
                ),
                _date_iso(
                    _first(record, "CurrentPeriodEndDate", "current_period_end_date", "CurPerEn")
                ),
            )
        )
    return _NormalizedRows(rows=rows, rejected_count=rejected_count, excluded_count=excluded_count)


def _master_rows_with_quality(records: Iterable[Mapping[str, Any]]) -> _NormalizedRows:
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
        snapshot_date = _date_iso(_first(record, "Date", "date", "snapshot_date")) or "unknown"
        is_common_stock = _is_common_stock_flag(record)
        sector_raw = _to_str_or_none(
            _first(record, "Sector33CodeName", "sector_33", "Sector33Name", "S33Nm", "S33")
        )
        rows.append(
            (
                snapshot_date,
                ticker,
                _to_str_or_none(_first(record, "CompanyName", "company_name", "Name", "CoName")),
                _to_str_or_none(_first(record, "MarketCodeName", "market_segment", "MktNm", "Mkt")),
                # J-Quants は同じ TSE 33 セクターを半角中黒 (U+FF65)・全角中黒 (U+30FB) で
                # 揺らせて返してくる。SQLite に取り込む段階で全角形に正規化し、macro context /
                # candidates / select の matcher が一意に解決できるようにする。
                normalize_sector_name(sector_raw) if sector_raw else sector_raw,
                1 if is_common_stock else 0,
            )
        )
    return _NormalizedRows(rows=rows, rejected_count=rejected_count, excluded_count=excluded_count)
