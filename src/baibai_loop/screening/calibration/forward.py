"""Forward return: asof から horizon 先までの価格リターンを asof-basis で計測する。

価格は `asof_basis_closes` (adjustment_factor 累積) で分割・併合を正規化する。
incremental cache の `adjustment_close` は遡及調整が混在するため使わない。
上場廃止・長期欠測は「target 直近の bar が古すぎる」事実として `stale_price`
flag で残し、黙って落とさない (ゼロ扱いもしない) 。配当は WU2 で total return
化するまで含まない (price-only。評価側の一次基準は母集団中央値対比のため、
横断比較には carry がほぼ相殺される) 。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import date, timedelta
from pathlib import Path

from baibai_loop.market.bars import asof_basis_closes
from baibai_loop.market.benchmark import TOPIX_ETF_PROXY

from ..providers.jquants import JQuantsDailyBar

# horizon 名 → 暦日数。target は asof + days の on-or-before 営業日に解決する。
HORIZONS: Mapping[str, int] = {"3m": 91, "6m": 182, "12m": 365}

# target の on-or-before 解決で許容する鮮度。これより古い bar しか無い場合は
# stale_price=True (上場廃止・長期売買停止の疑い) として計上する。
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


FORWARD_FIELD_NAMES: tuple[str, ...] = tuple(field.name for field in fields(ForwardReturnRow))


def compute_forward_returns(
    sqlite_path: Path,
    *,
    asofs: Sequence[date],
    tickers: Iterable[str],
    horizons: Mapping[str, int] = HORIZONS,
) -> list[ForwardReturnRow]:
    """Compute forward returns for every (asof, ticker, horizon).

    銘柄ごとに bar 系列を 1 回だけ読み、asof-basis の正規化 close で
    entry (asof on-or-before) と exit (target on-or-before) を解決する。
    eval cap (cache の最終営業日) を超える target は resolved=False。
    """
    if not asofs:
        return []
    unique_tickers = sorted(set(tickers) | set(BENCHMARK_TICKERS))
    min_asof = min(asofs)
    eval_cap = _latest_bar_date(sqlite_path)
    rows: list[ForwardReturnRow] = []
    conn = sqlite3.connect(sqlite_path)
    try:
        for ticker in unique_tickers:
            bars = _load_ticker_bars(conn, ticker, start=min_asof)
            rows.extend(
                _ticker_forward_rows(
                    ticker, bars, asofs=asofs, horizons=horizons, eval_cap=eval_cap
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
    horizons: Mapping[str, int],
    eval_cap: date | None,
) -> list[ForwardReturnRow]:
    dates = [bar.traded_at for bar in bars]
    closes = asof_basis_closes(bars) if bars else []
    rows: list[ForwardReturnRow] = []
    for asof in asofs:
        entry_index = _index_on_or_before(dates, asof)
        entry_close = closes[entry_index] if entry_index is not None else None
        entry_date = dates[entry_index] if entry_index is not None else None
        # entry 自体も鮮度を要求する。asof から 15 日より古い bar しか無い銘柄は
        # その cohort では取引実態が無い (上場前・廃止後・長期停止) とみなす。
        entry_valid = (
            entry_close is not None
            and entry_close > 0
            and entry_date is not None
            and (asof - entry_date).days <= STALE_PRICE_MAX_LAG_DAYS
        )
        for horizon, days in horizons.items():
            target = asof + timedelta(days=days)
            if not entry_valid:
                rows.append(
                    ForwardReturnRow(
                        asof=asof.isoformat(),
                        ticker=ticker,
                        horizon=horizon,
                        target_date=target.isoformat(),
                        resolved=False,
                        price_return=None,
                        stale_price=False,
                        entry_date=entry_date.isoformat() if entry_date else None,
                        exit_date=None,
                    )
                )
                continue
            if eval_cap is None or target > eval_cap:
                rows.append(
                    ForwardReturnRow(
                        asof=asof.isoformat(),
                        ticker=ticker,
                        horizon=horizon,
                        target_date=target.isoformat(),
                        resolved=False,
                        price_return=None,
                        stale_price=False,
                        entry_date=entry_date.isoformat() if entry_date else None,
                        exit_date=None,
                    )
                )
                continue
            exit_index = _index_on_or_before(dates, target)
            exit_close = closes[exit_index] if exit_index is not None else None
            exit_date = dates[exit_index] if exit_index is not None else None
            if exit_close is None or exit_date is None or entry_close is None:
                rows.append(
                    ForwardReturnRow(
                        asof=asof.isoformat(),
                        ticker=ticker,
                        horizon=horizon,
                        target_date=target.isoformat(),
                        resolved=False,
                        price_return=None,
                        stale_price=False,
                        entry_date=entry_date.isoformat() if entry_date else None,
                        exit_date=None,
                    )
                )
                continue
            stale = (target - exit_date).days > STALE_PRICE_MAX_LAG_DAYS
            rows.append(
                ForwardReturnRow(
                    asof=asof.isoformat(),
                    ticker=ticker,
                    horizon=horizon,
                    target_date=target.isoformat(),
                    resolved=True,
                    price_return=exit_close / entry_close - 1,
                    stale_price=stale,
                    entry_date=entry_date.isoformat() if entry_date else None,
                    exit_date=exit_date.isoformat(),
                )
            )
    return rows


def _index_on_or_before(dates: Sequence[date], target: date) -> int | None:
    """Binary search: index of the latest date <= target, or None."""
    lo, hi = 0, len(dates)
    while lo < hi:
        mid = (lo + hi) // 2
        if dates[mid] <= target:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1 if lo > 0 else None


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
            traded_at=date.fromisoformat(str(traded_at)),
            close=float(close),
            turnover_value=None,
            adjustment_factor=float(adjustment_factor) if adjustment_factor is not None else None,
        )
        for traded_at, close, adjustment_factor in rows
    ]


def _latest_bar_date(sqlite_path: Path) -> date | None:
    conn = sqlite3.connect(sqlite_path)
    try:
        row = conn.execute("SELECT MAX(traded_at) FROM jquants_daily_bars").fetchone()
    finally:
        conn.close()
    return date.fromisoformat(str(row[0])) if row and row[0] is not None else None
