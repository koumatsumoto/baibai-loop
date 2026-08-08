"""Calendar-aware, fail-visible price-return forward observations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields, replace
from datetime import date, timedelta
from math import isfinite
from pathlib import Path

from baibai_engine.market.bars import asof_basis_closes
from baibai_engine.market.benchmark import TOPIX_ETF_PROXY

from ..providers.jquants import JQuantsDailyBar
from .horizons import HORIZONS, HorizonSpec, require_horizon

__all__ = ("HORIZONS", "ForwardReturnRow", "compute_forward_returns")

ForwardStatus = str
AdjustmentCoverage = str
TotalReturnStatus = str
TOTAL_RETURN_BASIS = "fy_actual_dividend_fiscal_year_end_window"
TOTAL_RETURN_STATUSES = frozenset(
    {
        "resolved",
        "unresolved_price_return",
        "unresolved_adjustment_factor",
        "unresolved_no_fy_observation",
        "unresolved_missing_dividend",
        "unresolved_invalid_dividend",
        "unresolved_invalid_total_return",
    }
)

STALE_PRICE_MAX_LAG_DAYS = 15
BENCHMARK_TICKERS: tuple[str, ...] = (TOPIX_ETF_PROXY,)


@dataclass(frozen=True, slots=True, kw_only=True)
class ForwardReturnRow:
    """One (asof, ticker, horizon) price observation and why it did not resolve.

    The row records what was observed; the coverage verdicts a cohort needs are
    derived from these observations at evaluation time so that every stored
    cohort is judged by the current contract rather than by whatever contract
    was in force when its cache was written.
    """

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
    adjustment_factor_coverage: AdjustmentCoverage = "unknown"
    realized_dividend_sum: float | None = None
    realized_dividend_fy_count: int = 0
    total_return: float | None = None
    total_return_status: TotalReturnStatus = "unresolved_price_return"
    total_return_basis: str = TOTAL_RETURN_BASIS


@dataclass(frozen=True, slots=True)
class _FYDividendObservation:
    fiscal_year_end: date
    disclosed_at: date
    dps_actual_annual: float | None


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
    # Entry resolution accepts a bar up to STALE_PRICE_MAX_LAG_DAYS before asof, so
    # the load window has to start that far ahead of the earliest asof. Loading from
    # the asof itself makes the tolerance unusable: a name that did not trade on the
    # asof date reads as having no entry at all, even though it traded days earlier.
    min_asof = min(asofs) - timedelta(days=STALE_PRICE_MAX_LAG_DAYS)
    eval_cap = _latest_bar_date(sqlite_path)
    rows: list[ForwardReturnRow] = []
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        for ticker in unique_tickers:
            rows.extend(
                _ticker_forward_rows(
                    ticker,
                    _load_ticker_bars(conn, ticker, start=min_asof),
                    fy_dividends=_load_fy_dividends(conn, ticker, cutoff=eval_cap),
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
    fy_dividends: Sequence[_FYDividendObservation] = (),
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
                    rows.append(replace(base, status="unresolved_missing_exit"))
                elif (target - exit_date).days > STALE_PRICE_MAX_LAG_DAYS:
                    rows.append(
                        replace(
                            base,
                            stale_price=True,
                            exit_date=exit_date.isoformat(),
                            status="unresolved_stale_exit",
                        )
                    )
                else:
                    assert entry_date is not None
                    price_return = exit_close / float(entry_close or 0.0) - 1
                    dividend_sum, dividend_count, total_return, total_status = (
                        _resolve_total_return(
                            bars,
                            fy_dividends,
                            entry_date=entry_date,
                            exit_date=exit_date,
                            entry_close=float(entry_close or 0.0),
                            price_return=price_return,
                            adjustment_coverage=adjustment,
                        )
                    )
                    rows.append(
                        replace(
                            base,
                            resolved=True,
                            price_return=price_return,
                            exit_date=exit_date.isoformat(),
                            status="resolved",
                            realized_dividend_sum=dividend_sum,
                            realized_dividend_fy_count=dividend_count,
                            total_return=total_return,
                            total_return_status=total_status,
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


def _load_fy_dividends(
    conn: sqlite3.Connection, ticker: str, *, cutoff: date | None
) -> list[_FYDividendObservation]:
    if cutoff is None:
        return []
    rows = conn.execute(
        "SELECT fiscal_year_end, disclosed_at, dps_actual_annual "
        "FROM jquants_fin_summaries "
        "WHERE ticker = ? AND fiscal_period = 'FY' AND fiscal_year_end IS NOT NULL "
        "AND disclosed_at <= ? ORDER BY fiscal_year_end, disclosed_at",
        (ticker, cutoff.isoformat()),
    ).fetchall()
    return [
        _FYDividendObservation(
            fiscal_year_end=date.fromisoformat(str(fiscal_year_end)),
            disclosed_at=date.fromisoformat(str(disclosed_at)),
            dps_actual_annual=(float(dps) if dps is not None else None),
        )
        for fiscal_year_end, disclosed_at, dps in rows
    ]


def _resolve_total_return(
    bars: Sequence[JQuantsDailyBar],
    observations: Sequence[_FYDividendObservation],
    *,
    entry_date: date,
    exit_date: date,
    entry_close: float,
    price_return: float,
    adjustment_coverage: AdjustmentCoverage,
) -> tuple[float | None, int, float | None, TotalReturnStatus]:
    """Resolve retrospective FY dividends without treating absent data as zero."""
    if adjustment_coverage != "complete":
        return None, 0, None, "unresolved_adjustment_factor"
    by_fiscal_year_end: dict[date, list[_FYDividendObservation]] = {}
    for observation in observations:
        if entry_date < observation.fiscal_year_end <= exit_date:
            by_fiscal_year_end.setdefault(observation.fiscal_year_end, []).append(observation)
    if not by_fiscal_year_end:
        return None, 0, None, "unresolved_no_fy_observation"

    selected: list[_FYDividendObservation] = []
    for fiscal_year_end in sorted(by_fiscal_year_end):
        disclosed = [
            observation
            for observation in by_fiscal_year_end[fiscal_year_end]
            if observation.dps_actual_annual is not None
        ]
        if not disclosed:
            return None, 0, None, "unresolved_missing_dividend"
        latest = max(disclosed, key=lambda observation: observation.disclosed_at)
        assert latest.dps_actual_annual is not None
        if not isfinite(latest.dps_actual_annual) or latest.dps_actual_annual < 0:
            return None, 0, None, "unresolved_invalid_dividend"
        selected.append(latest)

    basis_date = bars[-1].traded_at
    dividend_sum = sum(
        float(observation.dps_actual_annual or 0.0)
        * _cumulative_adjustment_factor_after(
            bars, after=observation.disclosed_at, asof_date=basis_date
        )
        for observation in selected
    )
    if not isfinite(dividend_sum) or entry_close <= 0:
        return None, 0, None, "unresolved_invalid_dividend"
    total_return = price_return + dividend_sum / entry_close
    if not isfinite(total_return) or total_return < -1:
        return None, 0, None, "unresolved_invalid_total_return"
    return dividend_sum, len(selected), total_return, "resolved"


def _cumulative_adjustment_factor_after(
    bars: Sequence[JQuantsDailyBar], *, after: date, asof_date: date
) -> float:
    """Match per-share facts to the final share basis used by asof_basis_closes."""
    factor = 1.0
    for bar in bars:
        if bar.traded_at <= after or bar.traded_at > asof_date:
            continue
        if bar.adjustment_factor in (None, 0.0, 1.0):
            continue
        assert bar.adjustment_factor is not None
        factor *= bar.adjustment_factor
    return factor


def _latest_bar_date(sqlite_path: Path) -> date | None:
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT MAX(traded_at) FROM jquants_daily_bars").fetchone()
    finally:
        conn.close()
    return date.fromisoformat(str(row[0])) if row and row[0] is not None else None
