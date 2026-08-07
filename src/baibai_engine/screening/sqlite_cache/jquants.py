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
    EmptyRangeReplacementError,
    record_range_source_coverage,
    record_source_coverage,
    replace_date_range,
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
        replace_date_range(
            conn,
            "jquants_fin_summaries",
            "disclosed_at",
            requested_start,
            requested_end,
            replacement_row_count=len(rows),
        )
        if rows:
            conn.executemany(
                """
                INSERT OR REPLACE INTO jquants_fin_summaries(
                  ticker, disclosed_at, forecast_eps, eps_ttm, bps, shares_outstanding,
                  sales, cfo, cash_eq, total_assets, equity, operating_profit, ordinary_profit,
                  profit, forecast_profit, forecast_ordinary_profit,
                  fiscal_period, fiscal_year_end, period_start, period_end,
                  dps_actual_annual, dps_forecast_annual,
                  treasury_shares, equity_to_asset_ratio
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


WEEKLY_MARGIN_SOURCE = "jquants_weekly_margin"
WEEKLY_MARGIN_OPERATION = "get_mkt_margin_interest"


def weekly_margin_coverage_key(week_end: date) -> str:
    iso = week_end.isoformat()
    return f"{WEEKLY_MARGIN_OPERATION}:{iso}..{iso}"


def store_jquants_weekly_margin(
    db_path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    week_end: date,
) -> int:
    """Persist one weekly balance date, including the fact that it was empty.

    Not every week has a balance date; the exchange skips some, and asking for one
    of those returns nothing. Recording the coverage row for an empty answer is
    what tells the next pass the week was already examined, so a skipped week is
    asked for once rather than on every run. The row set for a date is replaced
    whole, which is how a re-fetch corrects a partially stored week.
    """
    normalized = _weekly_margin_rows_with_quality(records, week_end)
    rows = normalized.rows
    conn = open_connection(db_path)
    try:
        iso = week_end.isoformat()
        conn.execute("BEGIN")
        held = int(
            conn.execute(
                "SELECT COUNT(*) FROM jquants_weekly_margin WHERE week_end = ?", (iso,)
            ).fetchone()[0]
            or 0
        )
        if not rows and held:
            raise EmptyRangeReplacementError(
                f"refusing to replace {held} stored jquants_weekly_margin row(s) "
                f"for {iso} with an empty payload"
            )
        conn.execute("DELETE FROM jquants_weekly_margin WHERE week_end = ?", (iso,))
        if rows:
            conn.executemany(
                """
                INSERT INTO jquants_weekly_margin(
                  week_end, ticker, long_vol, short_vol, long_std_vol, long_neg_vol,
                  short_std_vol, short_neg_vol, issue_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        persisted_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM jquants_weekly_margin WHERE week_end = ?", (iso,)
            ).fetchone()[0]
            or 0
        )
        record_source_coverage(
            conn,
            source=WEEKLY_MARGIN_SOURCE,
            operation=WEEKLY_MARGIN_OPERATION,
            coverage_key=weekly_margin_coverage_key(week_end),
            coverage_start=iso,
            coverage_end=iso,
            requested_start=iso,
            requested_end=iso,
            params={"date": iso},
            record_count=persisted_count,
            status=normalized.status,
            error=normalized.error,
        )
        conn.commit()
        return persisted_count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _weekly_margin_rows_with_quality(
    records: Iterable[Mapping[str, Any]], week_end: date
) -> NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    rejected_count = 0
    excluded_count = 0
    seen: set[str] = set()
    for record in records:
        ticker, quality = code_quality(first(record, "Code", "code"))
        if quality == "rejected":
            rejected_count += 1
            continue
        if quality == "excluded" or ticker is None:
            excluded_count += 1
            continue
        if ticker in seen:
            rejected_count += 1
            continue
        seen.add(ticker)
        rows.append(
            (
                week_end.isoformat(),
                ticker,
                to_float(first(record, "LongVol", "long_vol")),
                to_float(first(record, "ShrtVol", "shrt_vol")),
                to_float(first(record, "LongStdVol", "long_std_vol")),
                to_float(first(record, "LongNegVol", "long_neg_vol")),
                to_float(first(record, "ShrtStdVol", "shrt_std_vol")),
                to_float(first(record, "ShrtNegVol", "shrt_neg_vol")),
                to_str_or_none(first(record, "IssType", "iss_type")),
            )
        )
    return NormalizedRows(rows=rows, rejected_count=rejected_count, excluded_count=excluded_count)


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
                # TrShFY=期末自己株式数、EqAR=開示された自己資本比率。ShOutFY は自己株式を
                # 含む発行済株式総数、Eq は非支配株主持分を含む純資産なので、時価総額と
                # 自己資本比率をそれぞれ正しい分母で作るには両方が要る。
                to_float(first(record, "TrShFY", "treasury_shares", "TreasuryStock")),
                to_float(first(record, "EqAR", "equity_to_asset_ratio", "EquityToAssetRatio")),
            )
        )
    return NormalizedRows(rows=rows, rejected_count=rejected_count, excluded_count=excluded_count)
