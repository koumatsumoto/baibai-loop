"""Read-through helpers that pull screening inputs out of the SQLite cache
written by `rebuild_from_raw()`.

The functions are deliberately permissive: a missing SQLite file or a source
that has not been imported yet returns `None` so the caller can fall back to
the JSON cache (and ultimately the API). They never raise on absence.

Coverage detection uses the `raw_imports` audit table together with the
J-Quants chunk window encoded in each filename (`...-start_dt-YYYY-MM-DD-
end_dt-YYYY-MM-DD.json`). The recorded `min_date`/`max_date` cover the data
dates only, which is too narrow for sparse sources like fin_summaries; the
filename window is what tells us whether the API request bracket has been
imported.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import date
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


def read_eq_master(sqlite_path: Path) -> list[SecurityMaster] | None:
    """Return the master snapshot rows currently in SQLite, or `None` when
    the cache is unavailable or has not imported any master file.
    """
    if not sqlite_path.exists():
        return None
    conn = sqlite3.connect(sqlite_path)
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
    conn = sqlite3.connect(sqlite_path)
    try:
        if not _range_covered(conn, "jquants_daily_bars", start, end):
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
    conn = sqlite3.connect(sqlite_path)
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
    conn = sqlite3.connect(sqlite_path)
    try:
        if not _has_any_import(conn, "jquants_earnings_calendar"):
            return None
        rows = conn.execute(
            "SELECT raw_json FROM jquants_earnings_calendar "
            "WHERE announcement_date BETWEEN ? AND ? "
            "ORDER BY announcement_date, ticker",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return [json.loads(row[0]) for row in rows]


def read_market_calendar(
    sqlite_path: Path, start: date, end: date
) -> list[JQuantsMarketCalendarDay] | None:
    """Return market calendar days for `[start, end]`, or `None` if the
    cache cannot serve the range.
    """
    if not sqlite_path.exists():
        return None
    conn = sqlite3.connect(sqlite_path)
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
    conn = sqlite3.connect(sqlite_path)
    try:
        if not _date_imported(conn, "edinet_documents", on_date):
            return None
        rows = conn.execute(
            "SELECT raw_json FROM edinet_documents WHERE doc_date = ? ORDER BY doc_id",
            (on_date.isoformat(),),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return [json.loads(row[0]) for row in rows]


def read_edinet_metrics(
    sqlite_path: Path, asof_date: date
) -> Mapping[str, EdinetMetricRecord] | None:
    """Return EDINET metric records keyed by ticker for `asof_date`, or
    `None` if the cache cannot serve the date.
    """
    if not sqlite_path.exists():
        return None
    conn = sqlite3.connect(sqlite_path)
    try:
        if not _date_imported(conn, "edinet_metrics", asof_date):
            return None
        rows = conn.execute(
            "SELECT ticker, sales_ttm, ocf_ttm, debt, cash, ebitda_ttm, "
            "consolidation_basis, ttm_quality_ev_ebitda, ttm_quality_p_s, ttm_quality_pcfr, "
            "operating_profit_ttm, depreciation_and_amortization_ttm, capex_ttm, fcf_ttm, "
            "net_cash, equity, total_assets, ttm_quality_fcf, ttm_quality_net_cash, "
            "source_doc_id, document_type, capex_source, failure_reasons "
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
            "capex_source": row[21],
            "failure_reasons": json.loads(row[22]) if row[22] else [],
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
    conn = sqlite3.connect(sqlite_path)
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
    取引停止. Treat the raw import / source-name rows as cache coverage so
    stale backfill gating does not force a refetch just because a required
    source happened to be empty on that date.
    """
    if not sqlite_path.exists():
        return False
    conn = sqlite3.connect(sqlite_path)
    try:
        try:
            cur = conn.execute(
                "SELECT 1 FROM raw_imports WHERE source = ? "
                "AND min_date <= ? AND max_date >= ? LIMIT 1",
                ("jpx_regulation_flags", asof_date.isoformat(), asof_date.isoformat()),
            )
            if cur.fetchone() is not None:
                return True
            cur = conn.execute(
                "SELECT 1 FROM jpx_regulation_flags WHERE asof_date = ? LIMIT 1",
                (asof_date.isoformat(),),
            )
            if cur.fetchone() is not None:
                return True
            cur = conn.execute(
                "SELECT 1 FROM jpx_regulation_sources WHERE asof_date = ? LIMIT 1",
                (asof_date.isoformat(),),
            )
        except sqlite3.OperationalError:
            return False
        return cur.fetchone() is not None
    finally:
        conn.close()


def _has_any_import(conn: sqlite3.Connection, source: str) -> bool:
    try:
        cur = conn.execute("SELECT 1 FROM raw_imports WHERE source = ? LIMIT 1", (source,))
    except sqlite3.OperationalError:
        return False
    return cur.fetchone() is not None


def _date_imported(conn: sqlite3.Connection, source: str, on_date: date) -> bool:
    """True when `raw_imports` records that `source` has imported a file
    whose `[min_date, max_date]` window includes `on_date`. EDINET / JPX
    chunks are per-date, so this collapses to an equality check on the
    filename stem captured in `min_date`.
    """
    iso = on_date.isoformat()
    try:
        cur = conn.execute(
            "SELECT 1 FROM raw_imports WHERE source = ? "
            "AND min_date <= ? AND max_date >= ? LIMIT 1",
            (source, iso, iso),
        )
    except sqlite3.OperationalError:
        return False
    return cur.fetchone() is not None


# Filenames are produced from a `sorted(params.items())` join in the
# provider, so `end_dt` comes before `start_dt` alphabetically.
_CHUNK_WINDOW_RE = re.compile(r"end_dt-(\d{4}-\d{2}-\d{2}).*?start_dt-(\d{4}-\d{2}-\d{2})")


def _range_covered(conn: sqlite3.Connection, source: str, start: date, end: date) -> bool:
    """True when raw_imports records for `source` collectively span the
    requested range. We prefer the chunk window encoded in the filename
    (the API request bracket) over the data min/max — sparse sources like
    fin_summaries import a 31-day window even if only a few disclosure
    dates land inside it.
    """
    try:
        rows = conn.execute(
            "SELECT path, min_date, max_date FROM raw_imports WHERE source = ?",
            (source,),
        ).fetchall()
    except sqlite3.OperationalError:
        return False
    if not rows:
        return False
    starts: list[str] = []
    ends: list[str] = []
    for path_text, min_date, max_date in rows:
        window = _parse_chunk_window(path_text)
        if window is not None:
            chunk_start, chunk_end = window
        else:
            if not min_date or not max_date:
                continue
            chunk_start, chunk_end = min_date, max_date
        starts.append(chunk_start)
        ends.append(chunk_end)
    if not starts or not ends:
        return False
    earliest = min(starts)
    latest = max(ends)
    return earliest <= start.isoformat() and latest >= end.isoformat()


def _parse_chunk_window(path_text: str | None) -> tuple[str, str] | None:
    if not path_text:
        return None
    match = _CHUNK_WINDOW_RE.search(path_text)
    if match is None:
        return None
    # Regex captures (end_dt, start_dt) given the alphabetical filename
    # order. Return as (start, end) for the caller.
    return match.group(2), match.group(1)


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
