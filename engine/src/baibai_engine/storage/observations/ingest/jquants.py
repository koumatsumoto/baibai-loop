"""J-Quants fundamentals ingest: master snapshot and financial summaries."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from math import isfinite
from pathlib import Path
from typing import Any

from baibai_engine.market.financial_documents import required_disclosure_number
from baibai_engine.market.jquants_decode import provider_record_payload
from baibai_engine.market.margin_publication import (
    require_all_issues_daily_balance_date,
    require_legacy_weekly_balance_date,
)
from baibai_engine.market.master_snapshot import (
    MASTER_SOURCE,
    master_coverage_key,
    validate_master_snapshot,
)
from baibai_engine.storage.observations.convert import (
    NormalizedRows,
    code_quality,
    date_iso,
    distinct_source_rows,
    to_float,
    to_str_or_none,
)
from baibai_engine.storage.observations.coverage import (
    delete_date_range,
    record_range_source_coverage,
    record_source_coverage,
)
from baibai_engine.storage.observations.jquants_contract import (
    ALL_ISSUES_DAILY_MARGIN_SOURCE,
    MARGIN_ALERT_OPERATION,
    MARGIN_ALERT_SOURCE,
    SHORT_SALE_REPORT_OPERATION,
    SHORT_SALE_REPORT_SOURCE,
    WEEKLY_MARGIN_SOURCE,
    all_issues_daily_margin_coverage_key,
    weekly_margin_coverage_key,
)
from baibai_engine.storage.observations.schema import open_connection


def store_jquants_fin_summaries(
    db_path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    requested_start: date,
    requested_end: date,
    fetched_at_utc: str | None = None,
) -> int:
    if requested_start > requested_end:
        raise ValueError("financial request start must not follow end")
    normalized = _fin_summary_rows_with_quality(records)
    rows = normalized.rows
    if any(
        not requested_start.isoformat() <= str(row[1]) <= requested_end.isoformat() for row in rows
    ):
        raise ValueError("financial payload contains a disclosure date outside the request")
    conn = open_connection(db_path)
    try:
        delete_date_range(
            conn,
            "jquants_fin_summaries",
            "disclosed_at",
            requested_start,
            requested_end,
        )
        if rows:
            conn.executemany(
                """
                INSERT INTO jquants_fin_summaries(
                  ticker, disclosed_at, disclosure_number, document_payload
                ) VALUES (?, ?, ?, ?)
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
            fetched_at_utc=fetched_at_utc,
        )
        conn.commit()
        return persisted_count
    finally:
        conn.close()


def store_jquants_weekly_margin(
    db_path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    week_end: date,
    fetched_at_utc: str | None = None,
) -> int:
    """Persist one weekly balance date, including the fact that it was empty.

    Not every week has a balance date; the exchange skips some, and asking for one
    of those returns nothing. Recording the coverage row preserves what the fetch
    observed. The bootstrap planner uses its timestamp and the publication calendar
    to retry an empty response observed too early; an empty observed after
    publication is final and is not fetched on every run. The row set for a date is
    replaced whole, which is how a re-fetch corrects a prematurely stored week.
    """
    require_legacy_weekly_balance_date(week_end)
    normalized = _weekly_margin_rows_with_quality(records, week_end)
    rows = normalized.rows
    conn = open_connection(db_path)
    try:
        iso = week_end.isoformat()
        conn.execute("BEGIN")
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
            coverage_key=weekly_margin_coverage_key(week_end),
            coverage_start=iso,
            coverage_end=iso,
            record_count=persisted_count,
            status=normalized.status,
            error=normalized.error,
            fetched_at_utc=fetched_at_utc,
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
    fetched_at_utc: str | None = None,
) -> int:
    """Persist the daily designated-issue dataset by publication date."""
    normalized = _margin_alert_rows_with_quality(records, requested_start, requested_end)
    conn = open_connection(db_path)
    try:
        conn.execute("BEGIN")
        delete_date_range(
            conn,
            "jquants_margin_alerts",
            "publication_date",
            requested_start,
            requested_end,
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
            fetched_at_utc=fetched_at_utc,
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
    fetched_at_utc: str | None = None,
) -> int:
    """Persist one post-transition all-issues daily balance date."""
    require_all_issues_daily_balance_date(balance_date)
    normalized = _all_issues_daily_margin_rows_with_quality(records, balance_date)
    conn = open_connection(db_path)
    try:
        iso = balance_date.isoformat()
        conn.execute("BEGIN")
        conn.execute("DELETE FROM jquants_all_issues_daily_margin WHERE balance_date = ?", (iso,))
        if normalized.rows:
            conn.executemany(
                """
                INSERT INTO jquants_all_issues_daily_margin(
                  balance_date, ticker, long_vol, short_vol, long_std_vol, long_neg_vol,
                  short_std_vol, short_neg_vol, issue_type, publication_date,
                  long_value, short_value, long_std_value, long_neg_value,
                  short_std_value, short_neg_value
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            coverage_key=all_issues_daily_margin_coverage_key(balance_date),
            coverage_start=iso,
            coverage_end=iso,
            record_count=persisted,
            status=normalized.status,
            error=normalized.error,
            fetched_at_utc=fetched_at_utc,
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
    fetched_at_utc: str | None = None,
) -> int:
    """Persist a complete disclosure-date range without inventing missing reports.

    A complete empty response removes rows previously held for the requested
    disclosure dates and records that empty source observation.
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
            delete_date_range(
                conn,
                "jquants_short_sale_reports",
                "disclosed_at",
                cursor,
                cursor,
            )
            if day_rows:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO jquants_short_sale_reports(
                      disclosed_at, source_ordinal, calculated_at, ticker, short_seller_name,
                      discretionary_investment_contractor_name, investment_fund_name,
                      short_ratio, short_shares, short_trading_units,
                      previous_reported_at, previous_short_ratio, notes,
                      short_seller_address, discretionary_investment_contractor_address
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    day_rows,
                )
            record_source_coverage(
                conn,
                source=SHORT_SALE_REPORT_SOURCE,
                coverage_key=f"{SHORT_SALE_REPORT_OPERATION}:{iso}..{iso}",
                coverage_start=iso,
                coverage_end=iso,
                record_count=len(day_rows),
                status=normalized.status,
                error=normalized.error,
                fetched_at_utc=fetched_at_utc,
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
        ticker, quality = code_quality(record.get("Code"))
        if quality == "rejected":
            rejected_count += 1
            continue
        if quality == "excluded" or ticker is None:
            excluded_count += 1
            continue
        disclosed_at = date_iso(record.get("DiscDate"))
        calculated_at = date_iso(record.get("CalcDate"))
        names = tuple(_text_or_empty(record.get(key)) for key in ("SSName", "DICName", "FundName"))
        if disclosed_at is None or calculated_at is None or not any(names):
            rejected_count += 1
            continue
        source_ordinal = next_ordinal_by_date.get(disclosed_at, 0)
        next_ordinal_by_date[disclosed_at] = source_ordinal + 1
        # Notes is free text, not a cancellation flag. Preserve missing/invalid
        # ratios as source evidence; the L2 state reader decides usability.
        rows.append(
            (
                disclosed_at,
                source_ordinal,
                calculated_at,
                ticker,
                *names,
                to_float(record.get("ShrtPosToSO")),
                _to_int_or_none(record.get("ShrtPosShares")),
                _to_int_or_none(record.get("ShrtPosUnits")),
                date_iso(record.get("PrevRptDate")),
                to_float(record.get("PrevRptRatio")),
                to_str_or_none(record.get("Notes")),
                to_str_or_none(record.get("SSAddr")),
                to_str_or_none(record.get("DICAddr")),
            )
        )
    return NormalizedRows(rows=rows, rejected_count=rejected_count, excluded_count=excluded_count)


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
    for record in records:
        ticker, quality = code_quality(record.get("Code"))
        if quality == "rejected":
            rejected_count += 1
            continue
        if quality == "excluded" or ticker is None:
            excluded_count += 1
            continue
        if date_iso(record.get("Date")) != week_end.isoformat():
            rejected_count += 1
            continue
        rows.append(
            (
                week_end.isoformat(),
                ticker,
                to_float(record.get("LongVol")),
                to_float(record.get("ShrtVol")),
                to_float(record.get("LongStdVol")),
                to_float(record.get("LongNegVol")),
                to_float(record.get("ShrtStdVol")),
                to_float(record.get("ShrtNegVol")),
                to_str_or_none(record.get("IssType")),
            )
        )
    return NormalizedRows(
        rows=distinct_source_rows(rows, identity_columns=2, source="weekly margin"),
        rejected_count=rejected_count,
        excluded_count=excluded_count,
    )


def _margin_alert_rows_with_quality(
    records: Iterable[Mapping[str, Any]], requested_start: date, requested_end: date
) -> NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    rejected_count = 0
    excluded_count = 0
    required_payload_fields = (
        "AppDate",
        "PubReason",
        "ShrtOut",
        "ShrtOutChg",
        "ShrtOutRatio",
        "LongOut",
        "LongOutChg",
        "LongOutRatio",
        "SLRatio",
        "ShrtNegOut",
        "ShrtNegOutChg",
        "ShrtStdOut",
        "ShrtStdOutChg",
        "LongNegOut",
        "LongNegOutChg",
        "LongStdOut",
        "LongStdOutChg",
        "TSEMrgnRegCls",
    )
    for record in records:
        if any(key not in record for key in required_payload_fields):
            rejected_count += 1
            continue
        ticker, quality = code_quality(record.get("Code"))
        if quality == "rejected":
            rejected_count += 1
            continue
        if quality == "excluded" or ticker is None:
            excluded_count += 1
            continue
        publication_date = date_iso(record.get("PubDate"))
        if publication_date is None:
            rejected_count += 1
            continue
        try:
            publication_day = date.fromisoformat(publication_date)
        except ValueError:
            rejected_count += 1
            continue
        applied_date = date_iso(record.get("AppDate"))
        publication_reason = to_str_or_none(record.get("PubReason"))
        core_balances = (
            to_float(record.get("ShrtOut")),
            to_float(record.get("LongOut")),
            to_float(record.get("ShrtNegOut")),
            to_float(record.get("ShrtStdOut")),
            to_float(record.get("LongNegOut")),
            to_float(record.get("LongStdOut")),
        )
        if (
            not requested_start <= publication_day <= requested_end
            or applied_date is None
            or publication_reason is None
            or any(value is None or not isfinite(value) or value < 0.0 for value in core_balances)
        ):
            rejected_count += 1
            continue
        rows.append(
            (
                publication_date,
                ticker,
                applied_date,
                publication_reason,
                core_balances[0],
                to_float(record.get("ShrtOutChg")),
                to_float(record.get("ShrtOutRatio")),
                core_balances[1],
                to_float(record.get("LongOutChg")),
                to_float(record.get("LongOutRatio")),
                to_float(record.get("SLRatio")),
                core_balances[2],
                to_float(record.get("ShrtNegOutChg")),
                core_balances[3],
                to_float(record.get("ShrtStdOutChg")),
                core_balances[4],
                to_float(record.get("LongNegOutChg")),
                core_balances[5],
                to_float(record.get("LongStdOutChg")),
                to_str_or_none(record.get("TSEMrgnRegCls")),
            )
        )
    return NormalizedRows(
        rows=distinct_source_rows(rows, identity_columns=2, source="margin alerts"),
        rejected_count=rejected_count,
        excluded_count=excluded_count,
    )


def _all_issues_daily_margin_rows_with_quality(
    records: Iterable[Mapping[str, Any]], balance_date: date
) -> NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    rejected_count = 0
    excluded_count = 0
    for record in records:
        ticker, quality = code_quality(record.get("Code"))
        if quality == "rejected":
            rejected_count += 1
            continue
        if quality == "excluded" or ticker is None:
            excluded_count += 1
            continue
        payload_date = date_iso(record.get("Date"))
        if payload_date != balance_date.isoformat():
            rejected_count += 1
            continue
        balances = (
            to_float(record.get("LongVol")),
            to_float(record.get("ShrtVol")),
            to_float(record.get("LongStdVol")),
            to_float(record.get("LongNegVol")),
            to_float(record.get("ShrtStdVol")),
            to_float(record.get("ShrtNegVol")),
        )
        issue_type = to_str_or_none(record.get("IssType"))
        if issue_type is None or any(
            value is None or not isfinite(value) or value < 0.0 for value in balances
        ):
            rejected_count += 1
            continue
        rows.append(
            (
                balance_date.isoformat(),
                ticker,
                *balances,
                issue_type,
                date_iso(record.get("PubDate")),
                # Keep native balance values without unit conversion or inference
                # from shares and a market price. Their source missingness remains null.
                to_float(record.get("LongVal")),
                to_float(record.get("ShrtVal")),
                to_float(record.get("LongStdVal")),
                to_float(record.get("LongNegVal")),
                to_float(record.get("ShrtStdVal")),
                to_float(record.get("ShrtNegVal")),
            )
        )
    return NormalizedRows(
        rows=distinct_source_rows(rows, identity_columns=2, source="daily all-issues margin"),
        rejected_count=rejected_count,
        excluded_count=excluded_count,
    )


def store_jquants_master(
    db_path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    requested_asof: date,
    fetched_at_utc: str | None = None,
) -> int:
    validated = validate_master_snapshot(list(records), requested_asof)
    conn = open_connection(db_path)
    try:
        iso = requested_asof.isoformat()
        conn.execute("BEGIN")
        conn.execute("DELETE FROM jquants_master_snapshots WHERE snapshot_date = ?", (iso,))
        conn.execute(
            "DELETE FROM source_coverage WHERE source = ? "
            "AND coverage_start = ? AND coverage_end = ?",
            (MASTER_SOURCE, iso, iso),
        )
        conn.executemany(
            "INSERT INTO jquants_master_snapshots(snapshot_date, security_code, record_json) "
            "VALUES (?, ?, ?)",
            validated.rows,
        )
        persisted_count = validated.persisted_count
        record_source_coverage(
            conn,
            source=MASTER_SOURCE,
            coverage_key=master_coverage_key(requested_asof),
            coverage_start=iso,
            coverage_end=iso,
            record_count=persisted_count,
            status="ok",
            error=None,
            fetched_at_utc=fetched_at_utc,
        )
        conn.commit()
        return persisted_count
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _fin_summary_rows_with_quality(records: Iterable[Mapping[str, Any]]) -> NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    seen: dict[tuple[str, str, str], str] = {}
    rejected_count = 0
    excluded_count = 0
    for record in records:
        ticker, quality = code_quality(record.get("Code"))
        if quality == "rejected":
            rejected_count += 1
            continue
        if quality == "excluded":
            excluded_count += 1
            continue
        disclosed_at = date_iso(record.get("DiscDate"))
        if ticker is None or disclosed_at is None:
            rejected_count += 1
            continue
        disclosed_at = date.fromisoformat(disclosed_at).isoformat()
        # Store provider fields, not selected forecasts or a second numeric truth.
        # Source record JSON is not the original HTTP response or Bulk CSV bytes.
        key = (ticker, disclosed_at, required_disclosure_number(record))
        payload = provider_record_payload(record)
        if key in seen:
            if seen[key] != payload:
                raise ValueError(f"conflicting financial document identity: {key}")
            continue
        seen[key] = payload
        rows.append((*key, payload))
    return NormalizedRows(rows=rows, rejected_count=rejected_count, excluded_count=excluded_count)
