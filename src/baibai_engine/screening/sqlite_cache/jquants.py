"""J-Quants fundamentals ingest: master snapshot and financial summaries."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

from baibai_engine.market.jquants import JQuantsProviderError
from baibai_engine.market.sqlite.convert import (
    NormalizedRows,
    code_quality,
    date_iso,
    first,
    to_float,
    to_str_or_none,
)
from baibai_engine.market.sqlite.coverage import (
    delete_date_range,
    record_range_source_coverage,
    record_source_coverage,
)
from baibai_engine.market.sqlite.schema import open_connection
from baibai_engine.screening.master_snapshot import (
    MASTER_OPERATION,
    MASTER_SOURCE,
    master_coverage_key,
    validate_master_snapshot,
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
                  profit, forecast_profit, forecast_ordinary_profit,
                  fiscal_period, fiscal_year_end, period_start, period_end,
                  dps_actual_annual, dps_forecast_annual
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


def store_jquants_master(
    db_path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    requested_asof: date,
) -> int:
    # Materialize and validate the complete response before opening SQLite. An
    # invalid provider batch must not create a DB or begin a transaction that
    # can disturb an already accepted snapshot.
    records_list = list(records)
    validated = validate_master_snapshot(records_list, requested_asof)
    conn = open_connection(db_path)
    try:
        iso = requested_asof.isoformat()
        conn.execute("BEGIN")
        conn.execute(
            "DELETE FROM jquants_master_snapshots WHERE snapshot_date = ?",
            (iso,),
        )
        # Remove coverage only for this requested snapshot. This also cleans a
        # same-date legacy key while retaining every other captured date.
        conn.execute(
            "DELETE FROM source_coverage WHERE source = ? "
            "AND coverage_start = ? AND coverage_end = ?",
            (MASTER_SOURCE, iso, iso),
        )
        conn.executemany(
            """
            INSERT INTO jquants_master_snapshots(
              snapshot_date, ticker, name, market, sector_33, is_common_stock
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            validated.rows,
        )
        count_row = conn.execute(
            "SELECT COUNT(*) FROM jquants_master_snapshots WHERE snapshot_date = ?",
            (iso,),
        ).fetchone()
        persisted_count = int(count_row[0] or 0)
        if persisted_count != validated.persisted_count:
            raise JQuantsProviderError(
                "J-Quants master persisted count mismatch before coverage write: "
                f"expected={validated.persisted_count} actual={persisted_count}"
            )
        if validated.raw_count != persisted_count + validated.excluded_count:
            raise JQuantsProviderError(
                "J-Quants master raw/persisted/excluded count mismatch after insert: "
                f"raw={validated.raw_count} persisted={persisted_count} "
                f"excluded={validated.excluded_count}"
            )
        common_row = conn.execute(
            "SELECT COUNT(*) FROM jquants_master_snapshots "
            "WHERE snapshot_date = ? AND is_common_stock = 1",
            (iso,),
        ).fetchone()
        persisted_common_count = int(common_row[0] or 0)
        if persisted_common_count != validated.common_stock_count:
            raise JQuantsProviderError(
                "J-Quants master common-stock count mismatch after insert: "
                f"expected={validated.common_stock_count} actual={persisted_common_count}"
            )
        record_source_coverage(
            conn,
            source=MASTER_SOURCE,
            operation=MASTER_OPERATION,
            coverage_key=master_coverage_key(requested_asof),
            coverage_start=iso,
            coverage_end=iso,
            requested_start=iso,
            requested_end=iso,
            params={"date": iso},
            record_count=persisted_count,
            status="ok",
            error=None,
        )
        coverage = conn.execute(
            "SELECT coverage_start, coverage_end, record_count, status, error "
            "FROM source_coverage WHERE source = ? AND coverage_key = ?",
            (MASTER_SOURCE, master_coverage_key(requested_asof)),
        ).fetchone()
        if coverage != (iso, iso, persisted_count, "ok", None):
            raise JQuantsProviderError("J-Quants master coverage write did not round-trip exactly")
        conn.commit()
        return persisted_count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
        # 会社予想の純利益/経常のペアは forecast_eps と同一予想期から採る。当期予想 EPS
        # (FEPS) が埋まっていれば当期予想の FNP/FOdP、本決算開示で FEPS が空なら翌期
        # ガイダンスの NxFNp/NxFOdP を採る (forecast_eps の FEPS→NxFEPS と同じ期選択)。
        # 期をまたいだ比較 (当期 EPS 期 と翌期利益の突合) は一時益 flag の誤検出になるので混ぜない。
        if first(record, "FEPS") is not None:
            forecast_profit = to_float(first(record, "FNP"))
            forecast_ordinary_profit = to_float(first(record, "FOdP"))
        else:
            forecast_profit = to_float(first(record, "NxFNp"))
            forecast_ordinary_profit = to_float(first(record, "NxFOdP"))
        rows.append(
            (
                ticker,
                disclosed_at,
                # ClientV2 の fin-summary は短縮キーを返す。FEPS=当期予想 EPS は本決算
                # (FY)開示で空になり、翌期ガイダンスは NxFEPS に入る。FEPS→NxFEPS の順で
                # 各時点の最良 forward EPS(per_forward の基)を埋める。
                to_float(first(record, "FEPS", "NxFEPS")),
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
                forecast_profit,
                forecast_ordinary_profit,
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
                # DivAnn=実績年間 DPS (FY 開示)。予想年間は四半期開示の FDivAnn、
                # 本決算開示では進行期ガイダンスが NxFDivAnn に入る (FEPS→NxFEPS と同型)。
                to_float(first(record, "DivAnn")),
                to_float(first(record, "FDivAnn", "NxFDivAnn")),
            )
        )
    return NormalizedRows(rows=rows, rejected_count=rejected_count, excluded_count=excluded_count)
