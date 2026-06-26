"""J-Quants fundamentals ingest: master snapshot, fin summaries, earnings calendar."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

from baibai_loop.market.sqlite.convert import (
    NormalizedRows,
    code_quality,
    date_iso,
    first,
    is_common_stock_flag,
    to_float,
    to_str_or_none,
)
from baibai_loop.market.sqlite.coverage import (
    delete_date_range,
    delete_source_coverage,
    record_range_source_coverage,
    record_source_coverage,
    table_row_count,
)
from baibai_loop.market.sqlite.schema import open_connection
from baibai_loop.screening.providers.jquants import (
    normalize_sector_name,
)


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
        delete_date_range(
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
        persisted_count = record_range_source_coverage(
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
        delete_source_coverage(conn, "jquants_master_snapshots")
        if rows:
            conn.executemany(
                """
                INSERT OR REPLACE INTO jquants_master_snapshots(
                  snapshot_date, ticker, name, market, sector_33, is_common_stock
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        persisted_count = table_row_count(conn, "jquants_master_snapshots")
        dates = sorted({row[0] for row in rows if row[0] != "unknown"})
        record_source_coverage(
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
        delete_source_coverage(conn, "jquants_earnings_calendar")
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO jquants_earnings_calendar("
                "announcement_date, ticker"
                ") VALUES (?, ?)",
                rows,
            )
        persisted_count = table_row_count(conn, "jquants_earnings_calendar")
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
        record_source_coverage(
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


def _earnings_calendar_rows_with_quality(records: Iterable[Mapping[str, Any]]) -> NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    rejected_count = 0
    excluded_count = 0
    for record in records:
        ticker, code_status = code_quality(first(record, "Code", "code"))
        if code_status == "rejected":
            rejected_count += 1
            continue
        if code_status == "excluded":
            excluded_count += 1
            continue
        announcement_date = date_iso(
            first(record, "Date", "date", "AnnouncementDate", "announcement_date")
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
    return NormalizedRows(rows=rows, rejected_count=rejected_count, excluded_count=excluded_count)


def _fin_summary_rows_with_quality(records: Iterable[Mapping[str, Any]]) -> NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    rejected_count = 0
    excluded_count = 0
    for record in records:
        ticker, quality = code_quality(first(record, "Code", "code"))
        if quality == "rejected":
            rejected_count += 1
            continue
        if quality == "excluded":
            excluded_count += 1
            continue
        disclosed_at = date_iso(
            first(record, "DisclosedDate", "disclosed_at", "DiscDate", "disc_date", "Date")
        )
        if ticker is None or disclosed_at is None:
            rejected_count += 1
            continue
        rows.append(
            (
                ticker,
                disclosed_at,
                # 年次(FY)開示は当期 ForecastEPS が空で、新年度ガイダンスは
                # NextYearForecast* に入る。fall back して本決算銘柄の forward EPS を拾う。
                to_float(
                    first(
                        record,
                        "ForecastEPS",
                        "forecast_eps",
                        "FEPS",
                        "NextYearForecastEarningsPerShare",
                        "NextYearForecastEPS",
                        "next_year_forecast_eps",
                        "NextYearFEPS",
                        "NextFEPS",
                    )
                ),
                to_float(first(record, "EpsTtm", "eps_ttm", "EPS", "eps")),
                to_float(first(record, "BPS", "bps")),
                to_float(
                    first(
                        record,
                        "SharesOutstanding",
                        "shares_outstanding",
                        "IssuedShareEquityQuote",
                        "ShOutFY",
                        "AvgSh",
                    )
                ),
                to_float(first(record, "NetSales", "net_sales", "Sales", "sales")),
                to_float(
                    first(
                        record,
                        "CashFlowsFromOperatingActivities",
                        "cash_flows_from_operating_activities",
                        "OperatingCashFlow",
                        "operating_cash_flow",
                        "CFO",
                        "cfo",
                    )
                ),
                to_float(
                    first(
                        record,
                        "CashAndEquivalents",
                        "cash_and_equivalents",
                        "CashEq",
                        "cash_eq",
                    )
                ),
                to_float(first(record, "TotalAssets", "total_assets", "TA", "ta")),
                to_float(first(record, "Equity", "equity", "Eq", "eq")),
                to_float(first(record, "OperatingProfit", "operating_profit", "OP")),
                to_float(first(record, "OrdinaryProfit", "ordinary_profit", "OdP")),
                to_float(first(record, "Profit", "profit", "NP")),
                to_str_or_none(
                    first(record, "TypeOfCurrentPeriod", "type_of_current_period", "CurPerType")
                ),
                date_iso(
                    first(
                        record,
                        "CurrentFiscalYearEndDate",
                        "current_fiscal_year_end_date",
                        "CurFYEn",
                    )
                ),
                date_iso(
                    first(record, "CurrentPeriodStartDate", "current_period_start_date", "CurPerSt")
                ),
                date_iso(
                    first(record, "CurrentPeriodEndDate", "current_period_end_date", "CurPerEn")
                ),
            )
        )
    return NormalizedRows(rows=rows, rejected_count=rejected_count, excluded_count=excluded_count)


def _master_rows_with_quality(records: Iterable[Mapping[str, Any]]) -> NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    rejected_count = 0
    excluded_count = 0
    for record in records:
        ticker, code_status = code_quality(first(record, "Code", "code"))
        if code_status == "rejected":
            rejected_count += 1
            continue
        if code_status == "excluded":
            excluded_count += 1
            continue
        snapshot_date = date_iso(first(record, "Date", "date", "snapshot_date")) or "unknown"
        is_common_stock = is_common_stock_flag(record)
        sector_raw = to_str_or_none(
            first(record, "Sector33CodeName", "sector_33", "Sector33Name", "S33Nm", "S33")
        )
        rows.append(
            (
                snapshot_date,
                ticker,
                to_str_or_none(first(record, "CompanyName", "company_name", "Name", "CoName")),
                to_str_or_none(first(record, "MarketCodeName", "market_segment", "MktNm", "Mkt")),
                # J-Quants は同じ TSE 33 セクターを半角中黒 (U+FF65)・全角中黒 (U+30FB) で
                # 揺らせて返してくる。SQLite に取り込む段階で全角形に正規化し、macro context /
                # candidates / select の matcher が一意に解決できるようにする。
                normalize_sector_name(sector_raw) if sector_raw else sector_raw,
                1 if is_common_stock else 0,
            )
        )
    return NormalizedRows(rows=rows, rejected_count=rejected_count, excluded_count=excluded_count)
