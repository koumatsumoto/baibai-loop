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

import re
import sqlite3
from datetime import date
from pathlib import Path

from .providers.jquants import (
    JQuantsDailyBar,
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
            "SELECT ticker, traded_at, close, turnover_value, adjustment_close "
            "FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ? "
            "ORDER BY ticker, traded_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()

    bars: list[JQuantsDailyBar] = []
    for ticker, traded_at, close, turnover_value, adjustment_close in rows:
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
            "shares_outstanding, sales, operating_profit, ordinary_profit, profit, "
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


def _has_any_import(conn: sqlite3.Connection, source: str) -> bool:
    try:
        cur = conn.execute("SELECT 1 FROM raw_imports WHERE source = ? LIMIT 1", (source,))
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
    except TypeError, ValueError:
        return None


def _optional_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None
