"""Calendar-aware, fail-visible price-return forward observations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields, replace
from datetime import date
from pathlib import Path

from baibai_engine.market.bars import asof_basis_closes
from baibai_engine.market.benchmark import TOPIX_ETF_PROXY

from ..providers.jquants import JQuantsDailyBar
from .horizons import HORIZONS, HorizonSpec, require_horizon

__all__ = ("HORIZONS", "ForwardReturnRow", "compute_forward_returns")

ForwardStatus = str
CoverageStatus = str
AdjustmentCoverage = str

STALE_PRICE_MAX_LAG_DAYS = 15
BENCHMARK_TICKERS: tuple[str, ...] = (TOPIX_ETF_PROXY,)


@dataclass(frozen=True, slots=True, kw_only=True)
class ForwardReturnRow:
    asof: str
    ticker: str
    horizon: str
    target_date: str
    resolved: bool
    price_return: float | None
    stale_price: bool
    entry_date: str | None
    exit_date: str | None
    status: ForwardStatus = "resolved"
    delisting_coverage_status: CoverageStatus = "not_assessed"
    corporate_action_event_coverage_status: CoverageStatus = "not_assessed"
    survivorship_coverage_status: CoverageStatus = "not_assessed"
    adjustment_factor_coverage: AdjustmentCoverage = "unknown"


FORWARD_FIELD_NAMES: tuple[str, ...] = tuple(field.name for field in fields(ForwardReturnRow))


def compute_forward_returns(
    sqlite_path: Path,
    *,
    asofs: Sequence[date],
    tickers: Iterable[str],
    horizons: Sequence[str] = tuple(HORIZONS),
) -> list[ForwardReturnRow]:
    if not asofs:
        return []
    specs = tuple(require_horizon(name) for name in horizons)
    unique_tickers = sorted(set(tickers) | set(BENCHMARK_TICKERS))
    min_asof = min(asofs)
    eval_cap = _latest_bar_date(sqlite_path)
    rows: list[ForwardReturnRow] = []
    conn = sqlite3.connect(sqlite_path)
    try:
        for ticker in unique_tickers:
            rows.extend(
                _ticker_forward_rows(
                    ticker,
                    _load_ticker_bars(conn, ticker, start=min_asof),
                    asofs=asofs,
                    horizons=specs,
                    eval_cap=eval_cap,
                )
            )
    finally:
        conn.close()
    return rows


def _ticker_forward_rows(
    ticker: str,
    bars: Sequence[JQuantsDailyBar],
    *,
    asofs: Sequence[date],
    horizons: Sequence[HorizonSpec],
    eval_cap: date | None,
) -> list[ForwardReturnRow]:
    dates = [bar.traded_at for bar in bars]
    closes = asof_basis_closes(bars) if bars else []
    rows: list[ForwardReturnRow] = []
    adjustment: AdjustmentCoverage
    if not bars or all(bar.adjustment_factor is None for bar in bars):
        adjustment = "unknown"
    elif all(bar.adjustment_factor is not None for bar in bars):
        adjustment = "complete"
    else:
        adjustment = "incomplete"
    specs = tuple(horizons)
    for asof in asofs:
        entry_index = _index_on_or_before(dates, asof)
        entry_date = dates[entry_index] if entry_index is not None else None
        entry_close = closes[entry_index] if entry_index is not None else None
        entry_valid = bool(
            entry_close
            and entry_close > 0
            and entry_date
            and (asof - entry_date).days <= STALE_PRICE_MAX_LAG_DAYS
        )
        for spec in specs:
            target = spec.target_date(asof)
            # One fully typed row per (asof, horizon), narrowed below. Spreading a dict
            # of the shared fields would erase the literal types every status relies on.
            base = ForwardReturnRow(
                asof=asof.isoformat(),
                ticker=ticker,
                horizon=spec.name,
                target_date=target.isoformat(),
                entry_date=entry_date.isoformat() if entry_date else None,
                resolved=False,
                price_return=None,
                stale_price=False,
                exit_date=None,
                status="unresolved_missing_entry",
                adjustment_factor_coverage=adjustment,
            )
            if not entry_valid:
                rows.append(base)
            elif eval_cap is None or target > eval_cap:
                rows.append(replace(base, status="unresolved_future_horizon"))
            else:
                exit_index = _index_on_or_before(dates, target)
                exit_date = dates[exit_index] if exit_index is not None else None
                exit_close = closes[exit_index] if exit_index is not None else None
                if exit_close is None or exit_date is None:
                    rows.append(
                        replace(
                            base,
                            delisting_coverage_status="unknown",
                            status="unresolved_missing_exit",
                        )
                    )
                elif (target - exit_date).days > STALE_PRICE_MAX_LAG_DAYS:
                    rows.append(
                        replace(
                            base,
                            delisting_coverage_status="unknown",
                            stale_price=True,
                            exit_date=exit_date.isoformat(),
                            status="unresolved_stale_exit",
                        )
                    )
                else:
                    rows.append(
                        replace(
                            base,
                            resolved=True,
                            price_return=exit_close / float(entry_close or 0.0) - 1,
                            exit_date=exit_date.isoformat(),
                            status="resolved",
                        )
                    )
    return rows


def _index_on_or_before(dates: Sequence[date], target: date) -> int | None:
    lo, hi = 0, len(dates)
    while lo < hi:
        mid = (lo + hi) // 2
        if dates[mid] <= target:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1 if lo else None


def _load_ticker_bars(
    conn: sqlite3.Connection, ticker: str, *, start: date
) -> list[JQuantsDailyBar]:
    rows = conn.execute(
        "SELECT traded_at, close, adjustment_factor FROM jquants_daily_bars "
        "WHERE ticker = ? AND traded_at >= ? AND close IS NOT NULL ORDER BY traded_at",
        (ticker, start.isoformat()),
    ).fetchall()
    return [
        JQuantsDailyBar(
            ticker=ticker,
            traded_at=date.fromisoformat(str(day)),
            close=float(close),
            turnover_value=None,
            adjustment_factor=float(factor) if factor is not None else None,
        )
        for day, close, factor in rows
    ]


def _latest_bar_date(sqlite_path: Path) -> date | None:
    conn = sqlite3.connect(sqlite_path)
    try:
        row = conn.execute("SELECT MAX(traded_at) FROM jquants_daily_bars").fetchone()
    finally:
        conn.close()
    return date.fromisoformat(str(row[0])) if row and row[0] is not None else None
