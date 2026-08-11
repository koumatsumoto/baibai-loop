"""J-Quants fundamentals ingest: master snapshot and financial summaries."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from math import isfinite
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
from baibai_engine.screening.margin_publication import (
    require_all_issues_daily_balance_date,
    require_legacy_weekly_balance_date,
)
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
                  treasury_shares, equity_to_asset_ratio,
                  dividend_q1, dividend_interim, dividend_q3, dividend_year_end,
                  dividend_total_annual, average_shares
                ) VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
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
MARGIN_ALERT_SOURCE = "jquants_margin_alerts"
MARGIN_ALERT_OPERATION = "get_mkt_margin_alert_range"
ALL_ISSUES_DAILY_MARGIN_SOURCE = "jquants_all_issues_daily_margin"
ALL_ISSUES_DAILY_MARGIN_OPERATION = "get_mkt_margin_interest"

SHORT_SALE_REPORT_SOURCE = "jquants_short_sale_reports"
SHORT_SALE_REPORT_OPERATION = "get_mkt_short_sale_report"


def weekly_margin_coverage_key(week_end: date) -> str:
    iso = week_end.isoformat()
    return f"{WEEKLY_MARGIN_OPERATION}:{iso}..{iso}"


def all_issues_daily_margin_coverage_key(balance_date: date) -> str:
    iso = balance_date.isoformat()
    return f"{ALL_ISSUES_DAILY_MARGIN_OPERATION}:{iso}..{iso}"


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
    require_legacy_weekly_balance_date(week_end)
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


def store_jquants_margin_alerts(
    db_path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    requested_start: date,
    requested_end: date,
) -> int:
    """Persist the daily designated-issue dataset by publication date."""
    normalized = _margin_alert_rows_with_quality(records, requested_start, requested_end)
    conn = open_connection(db_path)
    try:
        conn.execute("BEGIN")
        replace_date_range(
            conn,
            "jquants_margin_alerts",
            "publication_date",
            requested_start,
            requested_end,
            replacement_row_count=len(normalized.rows),
        )
        if normalized.rows:
            conn.executemany(
                """
                INSERT INTO jquants_margin_alerts(
                  publication_date, ticker, applied_date, publication_reason,
                  short_outstanding, short_change, short_ratio,
                  long_outstanding, long_change, long_ratio, short_long_ratio,
                  short_negotiable_outstanding, short_negotiable_change,
                  short_standard_outstanding, short_standard_change,
                  long_negotiable_outstanding, long_negotiable_change,
                  long_standard_outstanding, long_standard_change,
                  tse_margin_regulation_classification
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                normalized.rows,
            )
        persisted = record_range_source_coverage(
            conn,
            source=MARGIN_ALERT_SOURCE,
            operation=MARGIN_ALERT_OPERATION,
            table="jquants_margin_alerts",
            date_column="publication_date",
            requested_start=requested_start,
            requested_end=requested_end,
            status=normalized.status,
            error=normalized.error,
        )
        conn.commit()
        return persisted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def store_jquants_all_issues_daily_margin(
    db_path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    balance_date: date,
) -> int:
    """Persist one post-transition all-issues daily balance date."""
    require_all_issues_daily_balance_date(balance_date)
    normalized = _all_issues_daily_margin_rows_with_quality(records, balance_date)
    conn = open_connection(db_path)
    try:
        iso = balance_date.isoformat()
        conn.execute("BEGIN")
        held = int(
            conn.execute(
                "SELECT COUNT(*) FROM jquants_all_issues_daily_margin WHERE balance_date = ?",
                (iso,),
            ).fetchone()[0]
            or 0
        )
        if not normalized.rows and held:
            raise EmptyRangeReplacementError(
                f"refusing to replace {held} stored jquants_all_issues_daily_margin row(s) "
                f"for {iso} with an empty payload"
            )
        conn.execute("DELETE FROM jquants_all_issues_daily_margin WHERE balance_date = ?", (iso,))
        if normalized.rows:
            conn.executemany(
                """
                INSERT INTO jquants_all_issues_daily_margin(
                  balance_date, ticker, long_vol, short_vol, long_std_vol, long_neg_vol,
                  short_std_vol, short_neg_vol, issue_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                normalized.rows,
            )
        persisted = int(
            conn.execute(
                "SELECT COUNT(*) FROM jquants_all_issues_daily_margin WHERE balance_date = ?",
                (iso,),
            ).fetchone()[0]
            or 0
        )
        record_source_coverage(
            conn,
            source=ALL_ISSUES_DAILY_MARGIN_SOURCE,
            operation=ALL_ISSUES_DAILY_MARGIN_OPERATION,
            coverage_key=all_issues_daily_margin_coverage_key(balance_date),
            coverage_start=iso,
            coverage_end=iso,
            params={"date": iso, "publication_scope": "all_issues_daily"},
            record_count=persisted,
            status=normalized.status,
            error=normalized.error,
        )
        conn.commit()
        return persisted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def store_jquants_short_sale_reports(
    db_path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    requested_start: date,
    requested_end: date,
) -> int:
    """Persist a complete disclosure-date range without inventing missing reports.

    A successful empty response proves only that the endpoint returned no report
    rows for the requested disclosure dates. It records coverage, but an empty
    payload never replaces rows already held for that range.
    """
    normalized = _short_sale_report_rows_with_quality(records)
    rows = normalized.rows
    conn = open_connection(db_path)
    try:
        by_date: dict[str, list[tuple[Any, ...]]] = {}
        for row in rows:
            disclosed_at = str(row[0])
            if not requested_start.isoformat() <= disclosed_at <= requested_end.isoformat():
                raise ValueError(
                    "short-sale report response contains a disclosure date outside the request"
                )
            by_date.setdefault(disclosed_at, []).append(row)
        cursor = requested_start
        while cursor <= requested_end:
            iso = cursor.isoformat()
            day_rows = by_date.get(iso, [])
            replace_date_range(
                conn,
                "jquants_short_sale_reports",
                "disclosed_at",
                cursor,
                cursor,
                replacement_row_count=len(day_rows),
            )
            if day_rows:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO jquants_short_sale_reports(
                      disclosed_at, source_ordinal, calculated_at, ticker, short_seller_name,
                      discretionary_investment_contractor_name, investment_fund_name,
                      short_ratio, short_shares, short_trading_units,
                      previous_reported_at, previous_short_ratio, is_cancellation, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    day_rows,
                )
            record_source_coverage(
                conn,
                source=SHORT_SALE_REPORT_SOURCE,
                operation=SHORT_SALE_REPORT_OPERATION,
                coverage_key=f"{SHORT_SALE_REPORT_OPERATION}:{iso}..{iso}",
                coverage_start=iso,
                coverage_end=iso,
                params={"disclosed_date": iso},
                record_count=len(day_rows),
                status=normalized.status,
                error=normalized.error,
            )
            cursor += timedelta(days=1)
        persisted_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM jquants_short_sale_reports "
                "WHERE disclosed_at BETWEEN ? AND ?",
                (requested_start.isoformat(), requested_end.isoformat()),
            ).fetchone()[0]
            or 0
        )
        conn.commit()
        return persisted_count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _short_sale_report_rows_with_quality(
    records: Iterable[Mapping[str, Any]],
) -> NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    rejected_count = 0
    excluded_count = 0
    next_ordinal_by_date: dict[str, int] = {}
    for record in records:
        ticker, quality = code_quality(first(record, "Code", "code"))
        if quality == "rejected":
            rejected_count += 1
            continue
        if quality == "excluded" or ticker is None:
            excluded_count += 1
            continue
        disclosed_at = date_iso(
            first(record, "DiscDate", "DisclosedDate", "disclosed_at", "disc_date")
        )
        calculated_at = date_iso(
            first(record, "CalcDate", "CalculatedDate", "calculated_at", "calc_date")
        )
        short_ratio = to_float(
            first(
                record,
                "ShortPositionsToSharesOutstandingRatio",
                "ShortPosRatio",
                "ShrtPosToSO",
                "short_ratio",
            )
        )
        short_seller_name = _text_or_empty(
            first(record, "ShortSellerName", "SSName", "short_seller_name")
        )
        contractor_name = _text_or_empty(
            first(
                record,
                "DiscretionaryInvestmentContractorName",
                "DICName",
                "discretionary_investment_contractor_name",
            )
        )
        fund_name = _text_or_empty(
            first(record, "InvestmentFundName", "FundName", "investment_fund_name")
        )
        notes = to_str_or_none(first(record, "Notes", "notes"))
        is_cancellation = short_ratio is None and bool(notes and notes.strip())
        if (
            disclosed_at is None
            or calculated_at is None
            or (short_ratio is None and not is_cancellation)
            or (short_ratio is not None and short_ratio < 0.0)
            or not any((short_seller_name, contractor_name, fund_name))
        ):
            rejected_count += 1
            continue
        source_ordinal = next_ordinal_by_date.get(disclosed_at, 0)
        next_ordinal_by_date[disclosed_at] = source_ordinal + 1
        row = (
            disclosed_at,
            source_ordinal,
            calculated_at,
            ticker,
            short_seller_name,
            contractor_name,
            fund_name,
            short_ratio,
            _to_int_or_none(
                first(
                    record,
                    "ShortPositionsInSharesNumber",
                    "ShortPosShares",
                    "ShrtPosShares",
                    "short_shares",
                )
            ),
            _to_int_or_none(
                first(
                    record,
                    "ShortPositionsInTradingUnitsNumber",
                    "ShortPosTradingUnits",
                    "ShrtPosUnits",
                    "short_trading_units",
                )
            ),
            date_iso(
                first(
                    record,
                    "PrevRptDate",
                    "CalculationInPreviousReportingDate",
                    "previous_reported_at",
                )
            ),
            to_float(
                first(
                    record,
                    "ShortPositionsInPreviousReportingRatio",
                    "PrevShortPosRatio",
                    "PrevRptRatio",
                    "previous_short_ratio",
                )
            ),
            int(is_cancellation),
            notes,
        )
        rows.append(row)
    return NormalizedRows(
        rows=rows,
        rejected_count=rejected_count,
        excluded_count=excluded_count,
    )


def _to_int_or_none(value: Any) -> int | None:
    parsed = to_float(value)
    if parsed is None or not parsed.is_integer():
        return None
    return int(parsed)


def _text_or_empty(value: Any) -> str:
    return (to_str_or_none(value) or "").strip()


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


def _margin_alert_rows_with_quality(
    records: Iterable[Mapping[str, Any]], requested_start: date, requested_end: date
) -> NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    rejected_count = 0
    excluded_count = 0
    seen: set[tuple[str, str]] = set()
    for record in records:
        required_payload_fields = (
            ("AppDate", "ApplicationDate", "applied_date"),
            ("PubReason", "publication_reason"),
            ("ShrtOut", "short_outstanding"),
            ("ShrtOutChg", "short_change"),
            ("ShrtOutRatio", "short_ratio"),
            ("LongOut", "long_outstanding"),
            ("LongOutChg", "long_change"),
            ("LongOutRatio", "long_ratio"),
            ("SLRatio", "short_long_ratio"),
            ("ShrtNegOut", "short_negotiable_outstanding"),
            ("ShrtNegOutChg", "short_negotiable_change"),
            ("ShrtStdOut", "short_standard_outstanding"),
            ("ShrtStdOutChg", "short_standard_change"),
            ("LongNegOut", "long_negotiable_outstanding"),
            ("LongNegOutChg", "long_negotiable_change"),
            ("LongStdOut", "long_standard_outstanding"),
            ("LongStdOutChg", "long_standard_change"),
            ("TSEMrgnRegCls", "tse_margin_regulation_classification"),
        )
        if any(not any(key in record for key in aliases) for aliases in required_payload_fields):
            rejected_count += 1
            continue
        ticker, quality = code_quality(first(record, "Code", "code"))
        if quality == "rejected":
            rejected_count += 1
            continue
        if quality == "excluded" or ticker is None:
            excluded_count += 1
            continue
        publication_date = date_iso(first(record, "PubDate", "PublicationDate", "publication_date"))
        if publication_date is None:
            rejected_count += 1
            continue
        try:
            publication_day = date.fromisoformat(publication_date)
        except ValueError:
            rejected_count += 1
            continue
        identity = (publication_date, ticker)
        applied_date = date_iso(first(record, "AppDate", "ApplicationDate", "applied_date"))
        publication_reason = to_str_or_none(first(record, "PubReason", "publication_reason"))
        core_balances = (
            to_float(first(record, "ShrtOut", "short_outstanding")),
            to_float(first(record, "LongOut", "long_outstanding")),
            to_float(first(record, "ShrtNegOut", "short_negotiable_outstanding")),
            to_float(first(record, "ShrtStdOut", "short_standard_outstanding")),
            to_float(first(record, "LongNegOut", "long_negotiable_outstanding")),
            to_float(first(record, "LongStdOut", "long_standard_outstanding")),
        )
        if (
            not requested_start <= publication_day <= requested_end
            or identity in seen
            or applied_date is None
            or publication_reason is None
            or any(value is None or not isfinite(value) or value < 0.0 for value in core_balances)
        ):
            rejected_count += 1
            continue
        seen.add(identity)
        rows.append(
            (
                publication_date,
                ticker,
                applied_date,
                publication_reason,
                core_balances[0],
                to_float(first(record, "ShrtOutChg", "short_change")),
                to_float(first(record, "ShrtOutRatio", "short_ratio")),
                core_balances[1],
                to_float(first(record, "LongOutChg", "long_change")),
                to_float(first(record, "LongOutRatio", "long_ratio")),
                to_float(first(record, "SLRatio", "short_long_ratio")),
                core_balances[2],
                to_float(first(record, "ShrtNegOutChg", "short_negotiable_change")),
                core_balances[3],
                to_float(first(record, "ShrtStdOutChg", "short_standard_change")),
                core_balances[4],
                to_float(first(record, "LongNegOutChg", "long_negotiable_change")),
                core_balances[5],
                to_float(first(record, "LongStdOutChg", "long_standard_change")),
                to_str_or_none(
                    first(
                        record,
                        "TSEMrgnRegCls",
                        "tse_margin_regulation_classification",
                    )
                ),
            )
        )
    return NormalizedRows(rows=rows, rejected_count=rejected_count, excluded_count=excluded_count)


def _all_issues_daily_margin_rows_with_quality(
    records: Iterable[Mapping[str, Any]], balance_date: date
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
        payload_date = date_iso(first(record, "Date", "balance_date"))
        if payload_date != balance_date.isoformat() or ticker in seen:
            rejected_count += 1
            continue
        balances = (
            to_float(first(record, "LongVol", "long_vol")),
            to_float(first(record, "ShrtVol", "shrt_vol")),
            to_float(first(record, "LongStdVol", "long_std_vol")),
            to_float(first(record, "LongNegVol", "long_neg_vol")),
            to_float(first(record, "ShrtStdVol", "shrt_std_vol")),
            to_float(first(record, "ShrtNegVol", "shrt_neg_vol")),
        )
        issue_type = to_str_or_none(first(record, "IssType", "iss_type"))
        if issue_type is None or any(
            value is None or not isfinite(value) or value < 0.0 for value in balances
        ):
            rejected_count += 1
            continue
        seen.add(ticker)
        rows.append(
            (
                balance_date.isoformat(),
                ticker,
                *balances,
                issue_type,
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
                # 支払ごとの 1 株当たり配当。年間の DivAnn は中間・期末それぞれの基準日
                # 時点の株式基準で書かれるので、分割・併合を跨いだ年度は株価と基準が揃わ
                # ない。支払ごとに持てば、各支払の基準日より後の調整だけを掛けられる。
                to_float(first(record, "Div1Q")),
                to_float(first(record, "Div2Q")),
                to_float(first(record, "Div3Q")),
                to_float(first(record, "DivFY")),
                # DivTotalAnn=通期に支払った配当の総額 (円)。株式基準を持たないので、
                # 自己株式を除いた株式数で割った値が上の換算の独立した照合になる。
                to_float(first(record, "DivTotalAnn")),
                # AvgSh=期中平均株式数。提出者が EPS を出すのに使った株数そのもので、
                # 期末発行済から自己株を引いた株数が壊れていないかを同じ行の中で照合できる。
                to_float(first(record, "AvgSh")),
            )
        )
    return NormalizedRows(rows=rows, rejected_count=rejected_count, excluded_count=excluded_count)
