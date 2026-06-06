"""Read helpers that pull screening inputs from the canonical SQLite store.

The functions are deliberately permissive: a missing SQLite file or a source
that has not been fetched yet returns `None` so bootstrap/fetch commands can
populate the missing coverage. `screening run` performs a separate preflight
coverage check and must not fall back to provider APIs.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from .providers.edinet import EdinetMetricRecord, normalize_metric_record
from .providers.jpx import JPXRegulationSnapshot
from .providers.jquants import (
    JQuantsDailyBar,
    JQuantsFinancialSummary,
    JQuantsMarketCalendarDay,
    JQuantsProviderError,
)
from .schema import SecurityMaster
from .sqlite_cache import SQLiteSchemaError, validate_current_schema


def _connect_current(sqlite_path: Path) -> sqlite3.Connection | None:
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(sqlite_path)
        validate_current_schema(conn)
    except (SQLiteSchemaError, sqlite3.Error):
        if conn is not None:
            conn.close()
        return None
    return conn


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


def read_daily_bars(sqlite_path: Path, start: date, end: date) -> list[JQuantsDailyBar] | None:
    """Return daily bars for `[start, end]` from SQLite, or `None` if the
    cache cannot serve the full range.
    """
    if not sqlite_path.exists():
        return None
    conn = _connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not _daily_bars_covered_by_data(conn, start, end):
            return None
        rows = conn.execute(
            "SELECT ticker, traded_at, close, turnover_value, adjustment_close, adjustment_factor "
            "FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ? "
            "ORDER BY ticker, traded_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()

    bars: list[JQuantsDailyBar] = []
    for ticker, traded_at, close, turnover_value, adjustment_close, adjustment_factor in rows:
        if close is None or traded_at is None:
            continue
        try:
            bars.append(
                JQuantsDailyBar(
                    ticker=str(ticker),
                    traded_at=date.fromisoformat(traded_at),
                    close=float(close),
                    turnover_value=_optional_float(turnover_value),
                    adjustment_close=_optional_float(adjustment_close),
                    adjustment_factor=_optional_float(adjustment_factor),
                )
            )
        except (TypeError, ValueError) as exc:
            raise JQuantsProviderError(
                f"corrupt SQLite row in jquants_daily_bars for {ticker} on {traded_at}: {exc}"
            ) from exc
    return bars


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


def read_market_calendar(
    sqlite_path: Path, start: date, end: date
) -> list[JQuantsMarketCalendarDay] | None:
    """Return market calendar days for `[start, end]`, or `None` if the
    cache cannot serve the range.
    """
    if not sqlite_path.exists():
        return None
    conn = _connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not _range_covered(conn, "jquants_market_calendar", start, end):
            return None
        rows = conn.execute(
            "SELECT day, is_business_day FROM jquants_market_calendar "
            "WHERE day BETWEEN ? AND ? ORDER BY day",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return [
        JQuantsMarketCalendarDay(day=date.fromisoformat(day), is_business_day=bool(flag))
        for day, flag in rows
    ]


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


_RANGE_SOURCES_REQUIRING_ROWS = frozenset(
    {"jquants_daily_bars", "jquants_fin_summaries", "jquants_market_calendar"}
)


def _range_covered(conn: sqlite3.Connection, source: str, start: date, end: date) -> bool:
    """True when source_coverage rows collectively span the requested range.

    Used for fetch-provenance sources (financial summaries, market calendar)
    whose completeness cannot be re-derived from row presence: a missing filing
    is indistinguishable from "no filing was due". jquants_daily_bars is instead
    checked by `_daily_bars_covered_by_data`, because every trading day must carry
    a full-market row set, so its completeness IS observable from the data.
    """
    try:
        rows = conn.execute(
            "SELECT coverage_start, coverage_end, record_count, status "
            "FROM source_coverage WHERE source = ?",
            (source,),
        ).fetchall()
    except sqlite3.OperationalError:
        return False
    if not rows:
        return False
    intervals: list[tuple[date, date]] = []
    for coverage_start, coverage_end, record_count, status in rows:
        if status != "ok":
            continue
        if source in _RANGE_SOURCES_REQUIRING_ROWS and int(record_count or 0) == 0:
            continue
        if not coverage_start or not coverage_end:
            continue
        try:
            intervals.append((date.fromisoformat(coverage_start), date.fromisoformat(coverage_end)))
        except ValueError:
            continue
    if not intervals:
        return False
    intervals.sort()
    covered_until: date | None = None
    for chunk_start, chunk_end in intervals:
        if chunk_end < start:
            continue
        if chunk_start > end:
            break
        if covered_until is None:
            if chunk_start > start:
                return False
            covered_until = chunk_end
        elif chunk_start > covered_until + timedelta(days=1):
            return False
        else:
            covered_until = max(covered_until, chunk_end)
        if covered_until >= end:
            return True
    return False


# daily_bars completeness is derived from the actual rows (the single source of
# truth), not source_coverage. Every trading day carries a full-market row set,
# so a genuinely missing window shows up as a gap between present dates, while an
# interrupted fetch that left source_coverage holes but already wrote the rows
# must not trigger a re-fetch of data we hold. The only natural gaps are weekends
# and the Golden Week / New Year closures (observed max 7d), so a 10-day
# threshold separates complete history from a missing 31-day fetch chunk.
_DAILY_BARS_MAX_GAP_DAYS = 10
_DAILY_BARS_EDGE_TOLERANCE_DAYS = 10
_DAILY_BARS_COVERAGE_QUERY = (
    "SELECT DISTINCT traded_at FROM jquants_daily_bars "
    "WHERE traded_at BETWEEN ? AND ? ORDER BY traded_at"
)


def _daily_bars_covered_by_data(conn: sqlite3.Connection, start: date, end: date) -> bool:
    """True when the daily_bars rows themselves span `[start, end]`.

    Aggregates the actual table rather than consulting source_coverage, so data
    already saved is never re-fetched even when its bookkeeping row is missing.
    Covered means present trading dates reach both ends (within an edge
    tolerance) with no internal gap wider than a market holiday run.
    """
    if start > end:
        return False
    try:
        rows = conn.execute(
            _DAILY_BARS_COVERAGE_QUERY, (start.isoformat(), end.isoformat())
        ).fetchall()
    except sqlite3.OperationalError:
        return False
    dates: list[date] = []
    for (value,) in rows:
        if not value:
            continue
        try:
            dates.append(date.fromisoformat(value))
        except (TypeError, ValueError):
            continue
    if not dates:
        return False
    if (dates[0] - start).days > _DAILY_BARS_EDGE_TOLERANCE_DAYS:
        return False
    if (end - dates[-1]).days > _DAILY_BARS_EDGE_TOLERANCE_DAYS:
        return False
    return all(
        (current - previous).days <= _DAILY_BARS_MAX_GAP_DAYS
        for previous, current in pairwise(dates)
    )


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _optional_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None
