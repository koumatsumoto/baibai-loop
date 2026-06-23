"""Read helpers that pull screening fundamentals/regulation inputs from SQLite.

The functions are deliberately permissive: a missing SQLite file or a source
that has not been fetched yet returns `None` so bootstrap/fetch commands can
populate the missing coverage. `screening run` performs a separate preflight
coverage check and must not fall back to provider APIs. Price/calendar reads
live in `baibai_loop.market.store`; this module owns master / fin summaries /
earnings calendar / EDINET / JPX.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from baibai_loop.market.sqlite import (
    _connect_current,
    _optional_date,
    _optional_float,
    _range_covered,
)

from .providers.edinet import EdinetMetricRecord, normalize_metric_record
from .providers.jpx import JPXRegulationSnapshot
from .providers.jquants import (
    JQuantsFinancialSummary,
    JQuantsProviderError,
)
from .schema import SecurityMaster


def read_eq_master(sqlite_path: Path) -> list[SecurityMaster] | None:
    """Return the master snapshot rows currently in SQLite, or `None` when
    the cache is unavailable or has not imported any master file.
    """
    if not sqlite_path.exists():
        return None
    conn = _connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not _has_any_import(conn, "jquants_master_snapshots"):
            return None
        rows = conn.execute(
            "SELECT ticker, name, market, sector_33, is_common_stock "
            "FROM jquants_master_snapshots ORDER BY snapshot_date DESC, ticker"
        ).fetchall()
    finally:
        conn.close()

    seen: set[str] = set()
    masters: list[SecurityMaster] = []
    for ticker, name, market, sector_33, is_common in rows:
        if ticker in seen:
            # The table is keyed by (snapshot_date, ticker). When multiple
            # snapshots exist, prefer the most recent — the ORDER BY above
            # walks newest-first, so dropping repeats keeps the latest row.
            continue
        seen.add(ticker)
        masters.append(
            SecurityMaster(
                code=ticker,
                name=str(name or ""),
                market_segment=str(market or ""),
                sector_33=str(sector_33 or ""),
                is_common_stock=bool(is_common),
            )
        )
    return masters


def read_fin_summaries(
    sqlite_path: Path, start: date, end: date
) -> list[JQuantsFinancialSummary] | None:
    """Return financial summaries for `[start, end]` from SQLite, or `None`
    when the cache cannot serve the full range.
    """
    if not sqlite_path.exists():
        return None
    conn = _connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not _range_covered(conn, "jquants_fin_summaries", start, end):
            return None
        rows = conn.execute(
            "SELECT ticker, disclosed_at, forecast_eps, eps_ttm, bps, "
            "shares_outstanding, sales, cfo, cash_eq, total_assets, equity, "
            "operating_profit, ordinary_profit, profit, "
            "fiscal_period, fiscal_year_end, period_start, period_end "
            "FROM jquants_fin_summaries WHERE disclosed_at BETWEEN ? AND ? "
            "ORDER BY ticker, disclosed_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()

    summaries: list[JQuantsFinancialSummary] = []
    for row in rows:
        (
            ticker,
            disclosed_at,
            forecast_eps,
            eps_ttm,
            bps,
            shares_outstanding,
            sales,
            cfo,
            cash_eq,
            total_assets,
            equity,
            operating_profit,
            ordinary_profit,
            profit,
            fiscal_period,
            fiscal_year_end,
            period_start,
            period_end,
        ) = row
        try:
            summaries.append(
                JQuantsFinancialSummary(
                    ticker=str(ticker),
                    disclosed_at=date.fromisoformat(disclosed_at),
                    forecast_eps=_optional_float(forecast_eps),
                    eps_ttm=_optional_float(eps_ttm),
                    bps=_optional_float(bps),
                    shares_outstanding=_optional_float(shares_outstanding),
                    sales=_optional_float(sales),
                    cfo=_optional_float(cfo),
                    cash_eq=_optional_float(cash_eq),
                    total_assets=_optional_float(total_assets),
                    equity=_optional_float(equity),
                    operating_profit=_optional_float(operating_profit),
                    ordinary_profit=_optional_float(ordinary_profit),
                    profit=_optional_float(profit),
                    fiscal_period=fiscal_period if fiscal_period else None,
                    fiscal_year_end=_optional_date(fiscal_year_end),
                    period_start=_optional_date(period_start),
                    period_end=_optional_date(period_end),
                )
            )
        except (TypeError, ValueError) as exc:
            raise JQuantsProviderError(
                f"corrupt SQLite row in jquants_fin_summaries for {ticker}: {exc}"
            ) from exc
    return summaries


def read_eq_earnings_cal(sqlite_path: Path, start: date, end: date) -> list[dict[str, Any]] | None:
    """Return earnings calendar records overlapping `[start, end]`, or
    `None` when the cache cannot serve the range. Records are returned as
    raw dicts to match the JSON path's contract.
    """
    if not sqlite_path.exists():
        return None
    conn = _connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        # Fallback for historical replay: earnings calendar is a live-only endpoint
        # so past asof dates can't be matched exactly. Accept best-available data
        # if the forward horizon is covered by a more-recent fetch.
        exact = _minmax_covered(conn, "jquants_earnings_calendar", start, end)
        horizon = _minmax_horizon_covered(conn, "jquants_earnings_calendar", end)
        if not exact and not horizon:
            return None
        rows = conn.execute(
            "SELECT ticker, announcement_date FROM jquants_earnings_calendar "
            "WHERE announcement_date BETWEEN ? AND ? "
            "ORDER BY announcement_date, ticker",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return [{"Code": f"{ticker}0", "Date": announcement_date} for ticker, announcement_date in rows]


def read_edinet_documents(sqlite_path: Path, on_date: date) -> list[dict[str, Any]] | None:
    """Return raw EDINET document records for `on_date` from SQLite, or
    `None` if the cache cannot serve the date.
    """
    if not sqlite_path.exists():
        return None
    conn = _connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not _date_imported(conn, "edinet_documents", on_date):
            return None
        rows = conn.execute(
            "SELECT doc_id, sec_code, doc_type_code, csv_flag, xbrl_flag, legal_status, "
            "disclosure_status, withdrawal_status, submit_datetime, doc_description, "
            "period_start, period_end "
            "FROM edinet_documents WHERE doc_date = ? ORDER BY doc_id",
            (on_date.isoformat(),),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return [
        {
            "docID": doc_id,
            "secCode": sec_code,
            "docTypeCode": doc_type_code,
            "csvFlag": csv_flag,
            "xbrlFlag": xbrl_flag,
            "legalStatus": legal_status,
            "disclosureStatus": disclosure_status,
            "withdrawalStatus": withdrawal_status,
            "submitDateTime": submit_datetime,
            "docDescription": doc_description,
            "periodStart": period_start,
            "periodEnd": period_end,
        }
        for (
            doc_id,
            sec_code,
            doc_type_code,
            csv_flag,
            xbrl_flag,
            legal_status,
            disclosure_status,
            withdrawal_status,
            submit_datetime,
            doc_description,
            period_start,
            period_end,
        ) in rows
    ]


def read_edinet_metrics(
    sqlite_path: Path, asof_date: date
) -> Mapping[str, EdinetMetricRecord] | None:
    """Return EDINET metric records keyed by ticker for `asof_date`, or
    `None` if the cache cannot serve the date.
    """
    if not sqlite_path.exists():
        return None
    conn = _connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not _date_imported(conn, "edinet_metrics", asof_date):
            return None
        rows = conn.execute(
            "SELECT ticker, sales_ttm, ocf_ttm, debt, cash, ebitda_ttm, "
            "consolidation_basis, ttm_quality_ev_ebitda, ttm_quality_p_s, ttm_quality_pcfr, "
            "operating_profit_ttm, depreciation_and_amortization_ttm, capex_ttm, fcf_ttm, "
            "net_cash, equity, total_assets, ttm_quality_fcf, ttm_quality_net_cash, "
            "source_doc_id, document_type, source_submit_datetime, source_period_start, "
            "source_period_end, capex_source, failure_reasons "
            "FROM edinet_metrics WHERE asof_date = ?",
            (asof_date.isoformat(),),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()

    # Reuse the provider's normalize step so SQLite-backed records pick up
    # the same TTMQuality coercion / shape as the JSON path. Pass an
    # already-normalized payload that the function expects (typed columns
    # already match its key set).
    records: dict[str, EdinetMetricRecord] = {}
    for row in rows:
        payload = {
            "ticker": row[0],
            "sales_ttm": row[1],
            "ocf_ttm": row[2],
            "debt": row[3],
            "cash": row[4],
            "ebitda_ttm": row[5],
            "consolidation_basis": row[6],
            "ttm_quality_ev_ebitda": row[7],
            "ttm_quality_p_s": row[8],
            "ttm_quality_pcfr": row[9],
            "operating_profit_ttm": row[10],
            "depreciation_and_amortization_ttm": row[11],
            "capex_ttm": row[12],
            "fcf_ttm": row[13],
            "net_cash": row[14],
            "equity": row[15],
            "total_assets": row[16],
            "ttm_quality_fcf": row[17],
            "ttm_quality_net_cash": row[18],
            "source_doc_id": row[19],
            "document_type": row[20],
            "source_submit_datetime": row[21],
            "source_period_start": row[22],
            "source_period_end": row[23],
            "capex_source": row[24],
            "failure_reasons": json.loads(row[25]) if row[25] else [],
        }
        record = normalize_metric_record(payload)
        records[record.ticker] = record
    return records


def read_jpx_regulations(sqlite_path: Path, asof_date: date) -> JPXRegulationSnapshot | None:
    """Return the JPX regulation snapshot for `asof_date`, or `None` if the
    cache has not imported a snapshot for that date.
    """
    if not sqlite_path.exists():
        return None
    conn = _connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not _date_imported(conn, "jpx_regulation_flags", asof_date):
            return None
        rows = conn.execute(
            "SELECT source_name, ticker, flag FROM jpx_regulation_flags "
            "WHERE asof_date = ? ORDER BY ticker, flag",
            (asof_date.isoformat(),),
        ).fetchall()
        source_rows = conn.execute(
            "SELECT source_name FROM jpx_regulation_sources "
            "WHERE asof_date = ? ORDER BY source_name",
            (asof_date.isoformat(),),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()

    flags: dict[str, list[str]] = {}
    source_names: set[str] = {str(source_name) for (source_name,) in source_rows}
    for source_name, ticker, flag in rows:
        flags.setdefault(ticker, []).append(flag)
        source_names.add(source_name)
    return JPXRegulationSnapshot(
        flags_by_ticker={ticker: tuple(values) for ticker, values in flags.items()},
        source_names=tuple(sorted(source_names)),
    )


def has_jpx_regulation_data(sqlite_path: Path, asof_date: date) -> bool:
    """Return True when `asof_date` has an imported JPX regulation snapshot.

    A valid snapshot may have zero flagged tickers for a source such as
    取引停止. Treat `source_coverage` as the canonical cache coverage marker so
    stale backfill gating does not force a refetch just because a required
    source returned an empty source-specific table on that date.
    """
    if not sqlite_path.exists():
        return False
    conn = _connect_current(sqlite_path)
    if conn is None:
        return False
    try:
        try:
            cur = conn.execute(
                "SELECT 1 FROM source_coverage WHERE source = ? "
                "AND coverage_start <= ? AND coverage_end >= ? AND status = 'ok' LIMIT 1",
                ("jpx_regulation_flags", asof_date.isoformat(), asof_date.isoformat()),
            )
        except sqlite3.OperationalError:
            return False
        return cur.fetchone() is not None
    finally:
        conn.close()


def _has_any_import(conn: sqlite3.Connection, source: str) -> bool:
    try:
        cur = conn.execute(
            "SELECT 1 FROM source_coverage WHERE source = ? "
            "AND status = 'ok' AND record_count > 0 LIMIT 1",
            (source,),
        )
    except sqlite3.OperationalError:
        return False
    return cur.fetchone() is not None


def _date_imported(conn: sqlite3.Connection, source: str, on_date: date) -> bool:
    """True when `source_coverage` records `source` for `on_date`."""
    iso = on_date.isoformat()
    try:
        cur = conn.execute(
            "SELECT 1 FROM source_coverage WHERE source = ? "
            "AND coverage_start <= ? AND coverage_end >= ? AND status = 'ok' LIMIT 1",
            (source, iso, iso),
        )
    except sqlite3.OperationalError:
        return False
    return cur.fetchone() is not None


def _minmax_covered(conn: sqlite3.Connection, source: str, start: date, end: date) -> bool:
    """True when at least one coverage row brackets the requested range.

    Earnings calendar is cached as a whole-list endpoint rather than a
    request-windowed chunk. Sparse dates inside the window are valid, but a
    stale whole-list file whose actual min/max does not cover the requested
    horizon must fall back to the JSON/API path.
    """
    try:
        cur = conn.execute(
            "SELECT 1 FROM source_coverage WHERE source = ? "
            "AND coverage_start <= ? AND coverage_end >= ? "
            "AND status = 'ok' AND record_count > 0 LIMIT 1",
            (source, start.isoformat(), end.isoformat()),
        )
    except sqlite3.OperationalError:
        return False
    return cur.fetchone() is not None


def _minmax_horizon_covered(conn: sqlite3.Connection, source: str, end: date) -> bool:
    """True when any coverage row's end date covers the horizon, regardless of start.

    Used as a relaxed fallback for historical replay: the earnings calendar is a
    live-only endpoint so the exact start-date cannot be matched for past asof dates.
    Only checks that the forward horizon is covered by the available data.
    """
    try:
        cur = conn.execute(
            "SELECT 1 FROM source_coverage WHERE source = ? "
            "AND coverage_end >= ? AND status = 'ok' AND record_count > 0 LIMIT 1",
            (source, end.isoformat()),
        )
    except sqlite3.OperationalError:
        return False
    return cur.fetchone() is not None
