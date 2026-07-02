"""Single-ticker fact packet assembled from the SQLite store and recorded output.

The packet is the entry point for AI research on one security: price and
liquidity facts for any listed ticker (inside or outside the screening
universe), benchmark- and sector-relative momentum, the market regime at the
evaluation date, event flags relevant to the kill switch (next earnings, JPX
regulation), the ticker's latest recorded screening entry, and prior research
decisions. Every field is a deterministic transform of stored data; the packet
contains no interpretation and no composite score.

Valuation metrics are quoted from the recorded weekly candidates output rather
than recomputed, so the packet never disagrees with the screening facts; a
ticker without a candidates entry reports that absence explicitly.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from baibai_loop.foundation.yaml_io import safe_load

# position.trades is a standalone record reader (imports only foundation), so
# this screening->position import does not create a package cycle; the packet
# deliberately reads the trade records to expose portfolio-concentration facts.
from baibai_loop.position.trades import load_open_trades

from .regime import compute_market_regime
from .selection import load_prior_research

_BENCHMARK_TICKER = "1321"
_SELF_RANGE_WINDOW_BARS = 750
_WEEK_52_WINDOW_BARS = 252
_VOL_WINDOW_BARS = 20
_TURNOVER_WINDOW_BARS = 20
_RETURN_WINDOWS_BARS: tuple[int, ...] = (1, 5, 20, 60)
_PEER_RETURN_WINDOW_BARS = 20
_EARNINGS_HORIZON_DAYS = 90

_TICKER_LOOKBACK_CALENDAR_DAYS = 1130
_BENCHMARK_LOOKBACK_CALENDAR_DAYS = 100
_PEER_LOOKBACK_CALENDAR_DAYS = 45


@dataclass(frozen=True, slots=True)
class _Bar:
    traded_at: date
    price: float
    turnover_value: float | None


def build_ticker_profile(
    *,
    sqlite_path: Path,
    ticker: str,
    asof_date: date,
    candidates_root: Path,
    records_root: Path,
    benchmark_ticker: str = _BENCHMARK_TICKER,
) -> dict[str, object]:
    """Assemble the fact packet for ``ticker`` as of ``asof_date``."""
    bars = _load_bars(
        sqlite_path,
        tickers=(ticker,),
        start=asof_date - timedelta(days=_TICKER_LOOKBACK_CALENDAR_DAYS),
        end=asof_date,
    ).get(ticker, [])
    master = _load_master_row(sqlite_path, ticker)
    sector = master.get("sector_33") if master else None
    regime = compute_market_regime(sqlite_path, asof_date, benchmark_ticker=benchmark_ticker)
    candidates_block = _load_candidates_entry(candidates_root, ticker, asof_date)
    prior = load_prior_research(records_root / "03-thesis", asof_date).get(ticker)
    return {
        "ticker": ticker,
        "asof": asof_date.isoformat(),
        "master": master,
        "price": _price_block(bars),
        "relative": _relative_block(
            sqlite_path,
            ticker=ticker,
            bars=bars,
            sector=sector if isinstance(sector, str) else None,
            asof_date=asof_date,
            benchmark_ticker=benchmark_ticker,
        ),
        "regime": regime.to_dict() if regime is not None else None,
        "events": {
            "next_earnings_date": _next_earnings_date(sqlite_path, ticker, asof_date),
            "jpx_regulation": _jpx_flags(sqlite_path, ticker, asof_date),
        },
        "screening": candidates_block,
        "prior_research": prior.to_dict() if prior is not None else None,
        "portfolio": _portfolio_block(
            sqlite_path,
            repo_root=records_root.parent,
            ticker=ticker,
            sector=sector if isinstance(sector, str) else None,
        ),
    }


def _price_block(bars: Sequence[_Bar]) -> dict[str, object] | None:
    if not bars:
        return None
    prices = [bar.price for bar in bars]
    current = prices[-1]
    returns = {
        f"return_{window}d": _trailing_return(prices, window) for window in _RETURN_WINDOWS_BARS
    }
    window_52w = prices[-_WEEK_52_WINDOW_BARS:]
    high_52w = max(window_52w)
    low_52w = min(window_52w)
    range_window = prices[-_SELF_RANGE_WINDOW_BARS:]
    turnover = [
        bar.turnover_value
        for bar in bars[-_TURNOVER_WINDOW_BARS:]
        if bar.turnover_value is not None
    ]
    return {
        "resolved_date": bars[-1].traded_at.isoformat(),
        "close": current,
        "price_basis": "adjusted_close_preferred",
        **returns,
        "high_52w": high_52w,
        "low_52w": low_52w,
        "gap_from_52w_low": (current / low_52w - 1) if low_52w else None,
        "gap_from_52w_high": (current / high_52w - 1) if high_52w else None,
        "self_range_percentile": (
            sum(1 for price in range_window if price <= current) / len(range_window)
        ),
        "self_range_window_bars": len(range_window),
        "realized_vol_20d": _realized_vol(prices),
        "avg_turnover_20d_oku": (
            statistics.fmean(turnover) / 1e8
            if len(turnover) >= _TURNOVER_WINDOW_BARS // 2
            else None
        ),
        "bar_count": len(bars),
    }


def _relative_block(
    sqlite_path: Path,
    *,
    ticker: str,
    bars: Sequence[_Bar],
    sector: str | None,
    asof_date: date,
    benchmark_ticker: str,
) -> dict[str, object] | None:
    if not bars:
        return None
    prices = [bar.price for bar in bars]
    benchmark_bars = _load_bars(
        sqlite_path,
        tickers=(benchmark_ticker,),
        start=asof_date - timedelta(days=_BENCHMARK_LOOKBACK_CALENDAR_DAYS),
        end=asof_date,
    ).get(benchmark_ticker, [])
    benchmark_prices = [bar.price for bar in benchmark_bars]
    versus_benchmark = {}
    for window in _RETURN_WINDOWS_BARS:
        own = _trailing_return(prices, window)
        bench = _trailing_return(benchmark_prices, window)
        versus_benchmark[f"relative_{window}d"] = (
            own - bench if own is not None and bench is not None else None
        )
    return {
        "benchmark_ticker": benchmark_ticker,
        **versus_benchmark,
        "sector": _sector_block(sqlite_path, ticker=ticker, sector=sector, asof_date=asof_date)
        if sector
        else None,
    }


def _sector_block(
    sqlite_path: Path,
    *,
    ticker: str,
    sector: str,
    asof_date: date,
) -> dict[str, object] | None:
    peers = _sector_peers(sqlite_path, sector=sector, exclude=ticker)
    if not peers:
        return None
    bars_by_ticker = _load_bars(
        sqlite_path,
        tickers=tuple(peers),
        start=asof_date - timedelta(days=_PEER_LOOKBACK_CALENDAR_DAYS),
        end=asof_date,
    )
    returns = []
    for peer_bars in bars_by_ticker.values():
        peer_return = _trailing_return([bar.price for bar in peer_bars], _PEER_RETURN_WINDOW_BARS)
        if peer_return is not None:
            returns.append(peer_return)
    if not returns:
        return None
    return {
        "sector_33": sector,
        "peer_count": len(returns),
        "peer_median_return_20d": statistics.median(returns),
    }


def _portfolio_block(
    sqlite_path: Path,
    *,
    repo_root: Path,
    ticker: str,
    sector: str | None,
) -> dict[str, object]:
    """Concentration facts versus currently open positions.

    Entry notional uses each trade's recorded entry price x quantity (the same
    basis as the trade contract), so the sector share matches how the retro
    measures deployed concentration.
    """
    trades = load_open_trades(repo_root)
    positions = []
    total_notional = 0.0
    same_sector_notional = 0.0
    holds_this_ticker = False
    for trade in trades:
        master = _load_master_row(sqlite_path, trade.ticker)
        trade_sector = master.get("sector_33") if master else None
        notional = trade.entry_price * trade.quantity
        total_notional += notional
        if trade.ticker == ticker:
            holds_this_ticker = True
        if sector is not None and trade_sector == sector:
            same_sector_notional += notional
        positions.append(
            {
                "ticker": trade.ticker,
                "name": trade.name,
                "sector_33": trade_sector,
                "entry_notional_yen": round(notional),
            }
        )
    return {
        "open_position_count": len(positions),
        "holds_this_ticker": holds_this_ticker,
        "same_sector_position_count": sum(
            1 for position in positions if sector is not None and position["sector_33"] == sector
        ),
        "same_sector_entry_notional_share": (
            round(same_sector_notional / total_notional, 4) if total_notional else None
        ),
        "open_positions": positions,
    }


def _load_candidates_entry(
    candidates_root: Path,
    ticker: str,
    asof_date: date,
) -> dict[str, object]:
    latest: tuple[date, Path] | None = None
    if candidates_root.exists():
        for path in candidates_root.glob("*/*/*.yaml"):
            try:
                parsed = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if parsed <= asof_date and (latest is None or parsed > latest[0]):
                latest = (parsed, path)
    if latest is None:
        return {
            "candidates_ref": None,
            "in_candidates": False,
            "note": "no candidates file on or before asof",
        }
    payload = safe_load(latest[1].read_text(encoding="utf-8"))
    entries = payload.get("candidates") if isinstance(payload, Mapping) else None
    entry = None
    if isinstance(entries, Sequence):
        entry = next(
            (
                item
                for item in entries
                if isinstance(item, Mapping) and item.get("ticker") == ticker
            ),
            None,
        )
    block: dict[str, object] = {
        "candidates_ref": str(latest[1]),
        "candidates_asof": latest[0].isoformat(),
        "in_candidates": entry is not None,
    }
    if entry is not None:
        block["entry"] = dict(entry)
    else:
        block["note"] = (
            "ticker not present in the recorded candidates output "
            "(outside the screen scope or no playbook hit at that date)"
        )
    return block


def _next_earnings_date(sqlite_path: Path, ticker: str, asof_date: date) -> str | None:
    row = _query_one(
        sqlite_path,
        "SELECT MIN(announcement_date) FROM jquants_earnings_calendar "
        "WHERE ticker = ? AND announcement_date >= ? AND announcement_date <= ?",
        (
            ticker,
            asof_date.isoformat(),
            (asof_date + timedelta(days=_EARNINGS_HORIZON_DAYS)).isoformat(),
        ),
    )
    return row[0] if row and isinstance(row[0], str) else None


def _jpx_flags(sqlite_path: Path, ticker: str, asof_date: date) -> dict[str, object]:
    snapshot_row = _query_one(
        sqlite_path,
        "SELECT MAX(asof_date) FROM jpx_regulation_flags WHERE asof_date <= ?",
        (asof_date.isoformat(),),
    )
    snapshot_date = snapshot_row[0] if snapshot_row and isinstance(snapshot_row[0], str) else None
    if snapshot_date is None:
        return {"snapshot_date": None, "flags": []}
    rows = _query_all(
        sqlite_path,
        "SELECT DISTINCT flag FROM jpx_regulation_flags WHERE ticker = ? AND asof_date = ?",
        (ticker, snapshot_date),
    )
    return {
        "snapshot_date": snapshot_date,
        "flags": sorted(str(row[0]) for row in rows if row[0]),
    }


def _load_master_row(sqlite_path: Path, ticker: str) -> dict[str, object] | None:
    row = _query_one(
        sqlite_path,
        "SELECT name, market, sector_33, is_common_stock FROM jquants_master_snapshots "
        "WHERE ticker = ? ORDER BY snapshot_date DESC LIMIT 1",
        (ticker,),
    )
    if row is None:
        return None
    name, market, sector_33, is_common = row
    return {
        "name": str(name or ""),
        "market_segment": str(market or ""),
        "sector_33": str(sector_33 or ""),
        "is_common_stock": bool(is_common),
    }


def _sector_peers(sqlite_path: Path, *, sector: str, exclude: str) -> list[str]:
    rows = _query_all(
        sqlite_path,
        "SELECT DISTINCT ticker FROM jquants_master_snapshots WHERE sector_33 = ? AND ticker != ?",
        (sector, exclude),
    )
    return [str(row[0]) for row in rows]


def _trailing_return(prices: Sequence[float], window_bars: int) -> float | None:
    if len(prices) < window_bars + 1:
        return None
    past = prices[-(window_bars + 1)]
    if past == 0:
        return None
    return prices[-1] / past - 1


def _realized_vol(prices: Sequence[float]) -> float | None:
    window = prices[-(_VOL_WINDOW_BARS + 1) :]
    if len(window) < _VOL_WINDOW_BARS + 1:
        return None
    daily_returns = [
        window[index] / window[index - 1] - 1
        for index in range(1, len(window))
        if window[index - 1] != 0
    ]
    if len(daily_returns) < 2:
        return None
    return statistics.stdev(daily_returns) * math.sqrt(252)


def _load_bars(
    sqlite_path: Path,
    *,
    tickers: Sequence[str],
    start: date,
    end: date,
) -> dict[str, list[_Bar]]:
    if not tickers or not sqlite_path.exists():
        return {}
    # placeholders is only "?,?,..." markers; ticker values are bound parameters
    # in conn.execute, so the f-string is not an injection vector.
    placeholders = ",".join("?" for _ in tickers)
    query = (
        "SELECT ticker, traded_at, close, adjustment_factor, turnover_value "  # nosec B608
        "FROM jquants_daily_bars "
        f"WHERE traded_at >= ? AND traded_at <= ? AND ticker IN ({placeholders}) "
        "ORDER BY ticker, traded_at"
    )
    rows = _query_all(sqlite_path, query, (start.isoformat(), end.isoformat(), *tickers))
    # cache の adjustment_close は incremental 取得で遡及の有無が混在するため使わず、
    # 不変イベントの adjustment_factor の後方累積で末尾基準の価格系列を組む。
    raw: dict[str, list[tuple[date, float, float | None, float | None]]] = {}
    for ticker, traded_at, close, adjustment_factor, turnover_value in rows:
        if not isinstance(close, int | float) or not isinstance(traded_at, str):
            continue
        raw.setdefault(str(ticker), []).append(
            (
                date.fromisoformat(traded_at),
                float(close),
                float(adjustment_factor) if isinstance(adjustment_factor, int | float) else None,
                float(turnover_value) if isinstance(turnover_value, int | float) else None,
            )
        )
    bars: dict[str, list[_Bar]] = {}
    for ticker, entries in raw.items():
        factor = 1.0
        normalized: list[_Bar] = []
        for traded_at, close, adjustment_factor, turnover_value in reversed(entries):
            normalized.append(
                _Bar(traded_at=traded_at, price=close * factor, turnover_value=turnover_value)
            )
            if adjustment_factor not in (None, 0.0, 1.0):
                assert adjustment_factor is not None
                factor *= adjustment_factor
        normalized.reverse()
        bars[ticker] = normalized
    return bars


def _query_one(
    sqlite_path: Path,
    query: str,
    params: tuple[object, ...],
) -> tuple[object, ...] | None:
    rows = _query_all(sqlite_path, query, params)
    return rows[0] if rows else None


def _query_all(
    sqlite_path: Path,
    query: str,
    params: tuple[object, ...],
) -> list[tuple[object, ...]]:
    if not sqlite_path.exists():
        return []
    conn = sqlite3.connect(sqlite_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()
